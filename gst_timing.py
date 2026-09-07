"""Common media running-time and a bounded, isolated decoded-audio handoff."""

from collections import deque
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from gst_base import GstPipelineBase

MS = 1_000_000
# Shared output scheduling allowance, NOT the configurable extra NDI delay.
SCHEDULING_MARGIN_NS = 50 * MS


def video_output_chain(source, video_format, delay_ns, capacity_ns, *, deinterlace=False,
                       progressive=False, destination="combiner.video"):
    conversion = "videoconvert"
    if deinterlace:
        conversion += " ! deinterlace ! videoconvert"
    interlace = ",interlace-mode=progressive" if progressive or deinterlace else ""
    fix = "" if interlace else (
        "! capssetter name=video_interlace caps=video/x-raw,interlace-mode=interleaved replace=false "
    )
    return (
        f"{source} ! queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! {conversion} "
        f"! video/x-raw,format={video_format}{interlace} {fix}"
        f"! identity name=video_timeline single-segment=true sync=true silent=true signal-handoffs=false "
        f"! queue name=ndi_video_delay max-size-buffers=0 max-size-bytes=0 max-size-time={capacity_ns} "
        f"min-threshold-time={delay_ns} ! {destination} "
    )


def audio_output_chain(source, rate, channels, delay_ns, capacity_ns, *, destination="combiner.audio"):
    byte_limit = max(4096, rate * channels * 4 * (capacity_ns + 100 * MS) // Gst.SECOND)
    return (
        f"{source} ! queue ! audioconvert ! audioresample ! audiorate "
        f"! audio/x-raw,format=F32LE,rate={rate},channels={channels},layout=interleaved "
        f"! identity name=audio_timeline single-segment=true sync=true silent=true signal-handoffs=false "
        f"! queue name=ndi_audio_delay max-size-buffers=0 max-size-bytes={byte_limit} max-size-time={capacity_ns} "
        f"min-threshold-time={delay_ns} ! {destination} "
    )


def retimestamp_pcm(buffer, pts, discont=False):
    # PyGObject's boxed Buffer.copy() only refs the SAME buffer header. A region
    # copy creates a writable header while retaining references to native PCM.
    flags = Gst.BufferCopyFlags.FLAGS | Gst.BufferCopyFlags.TIMESTAMPS | Gst.BufferCopyFlags.META | Gst.BufferCopyFlags.MEMORY
    outgoing = buffer.copy_region(flags, 0, buffer.get_size())
    outgoing.pts = pts
    outgoing.dts = Gst.CLOCK_TIME_NONE
    if discont:
        outgoing.set_flags(Gst.BufferFlags.DISCONT)
    return outgoing


class AudioHandoff:
    """One consumer, no sink calls or waits on the producer's streaming thread."""

    def __init__(self, max_bytes, max_time_ns):
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.items = deque()
        self.max_bytes = max_bytes
        self.max_time_ns = max_time_ns
        self.bytes = 0
        self.active = False
        self.eos = False
        self.dropped = 0
        self.discont = True

    def activate(self):
        with self.lock:
            self.items.clear()
            self.bytes = 0
            self.eos = False
            self.active = True
            self.discont = True
            self.dropped = 0

    def deactivate(self):
        with self.lock:
            self.active = False
            self.items.clear()
            self.bytes = 0
        self.ready.set()

    def offer(self, buffer, caps, absolute_pts):
        if not self.active:
            return
        # Status/consumer contention must never block the NDI audio branch.
        if not self.lock.acquire(blocking=False):
            self.dropped += 1
            self.discont = True
            return
        try:
            if not self.active:
                return
            size = buffer.get_size()
            if size > self.max_bytes:
                self.dropped += 1
                self.discont = True
                return
            while self.items and (
                self.bytes + size > self.max_bytes or len(self.items) >= 64
                or absolute_pts - self.items[0][2] > self.max_time_ns
            ):
                old = self.items.popleft()
                self.bytes -= old[3]
                self.dropped += 1
                self.discont = True
            self.items.append((buffer, caps, absolute_pts, size))
            self.bytes += size
        finally:
            self.lock.release()
        self.ready.set()

    def finish(self):
        self.eos = True
        self.ready.set()

    def take(self):
        with self.lock:
            if not self.items:
                self.ready.clear()
                return None
            buffer, caps, pts, size = self.items.popleft()
            self.bytes -= size
            discont, self.discont = self.discont, False
            return buffer, caps, pts, discont

    def snapshot(self):
        with self.lock:
            return {"queued_bytes": self.bytes, "queued_buffers": len(self.items),
                    "max_bytes": self.max_bytes, "dropped_buffers": self.dropped}


class MediaTiming:
    """Segment-normalized A/V uses one system clock; NDI alone adds delay."""

    def __init__(self, delay_ns, queue_ns, rate, channels, handoff_ms=200):
        self.delay_ns = delay_ns
        self.queue_ns = queue_ns
        self.clock = Gst.SystemClock.obtain()
        self.source_latency_ns = SCHEDULING_MARGIN_NS
        self.latency_ready = threading.Event()
        self.audio_ready = threading.Event()
        self.pipeline = None
        self.probes = []
        self.caps = None
        # F32 PCM is fixed upstream of the tap; both bytes and time are bounded.
        handoff_ms = max(50, min(500, int(handoff_ms)))
        self.handoff = AudioHandoff(rate * channels * 4 * handoff_ms // 1000, handoff_ms * MS)

    def prepare(self, pipeline):
        self.pipeline = pipeline
        pipeline.use_clock(self.clock)
        # Discover native combiner/sink requirements before pinning the shared
        # baseline. Fixed latency set too early can undercut negotiated minima.
        pipeline.set_latency(Gst.CLOCK_TIME_NONE)
        audio = pipeline.get_by_name("audio_timeline")
        if audio is None:
            raise RuntimeError("Shared decoded audio timeline is missing")
        pad = audio.get_static_pad("src")
        probe = pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, self._audio)
        self.probes.append((pad, probe))
        video = pipeline.get_by_name("video_timeline")
        pad = video.get_static_pad("src")
        probe = pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._video)
        self.probes.append((pad, probe))
        fixer = pipeline.get_by_name("video_interlace")
        if fixer is not None:
            pad = fixer.get_static_pad("sink")
            probe = pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._interlace, fixer)
            self.probes.append((pad, probe))

    @staticmethod
    def _interlace(pad, info, fixer):
        event = info.get_event()
        if event.type == Gst.EventType.CAPS:
            caps = event.parse_caps()
            mode = caps.get_structure(0).get_string("interlace-mode")
            # Retain the mixed-mode decoder compatibility fix without relabelling
            # genuinely progressive frames as interlaced.
            override = ",interlace-mode=interleaved" if mode == "mixed" else ""
            fixer.set_property("caps", Gst.Caps.from_string("video/x-raw" + override))
        return Gst.PadProbeReturn.OK

    def _video(self, pad, info):
        event = info.get_event()
        if event.type == Gst.EventType.CAPS:
            caps = event.parse_caps()
            valid, numerator, denominator = caps.get_structure(0).get_fraction("framerate")
            if not valid or numerator <= 0 or denominator <= 0:
                numerator, denominator = 60, 1
            # A frame-count safety limit still bounds memory if a damaged source
            # stops providing usable timestamps. Leave two frames of headroom.
            divisor = denominator * Gst.SECOND
            frames = max(4, (self.queue_ns * numerator + divisor - 1) // divisor + 2)
            self.pipeline.get_by_name("ndi_video_delay").set_property("max-size-buffers", frames)
        return Gst.PadProbeReturn.OK

    def _audio(self, pad, info):
        if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
            event = info.get_event()
            if event.type == Gst.EventType.CAPS:
                self.caps = event.parse_caps()
            elif event.type == Gst.EventType.EOS:
                self.handoff.finish()
            return Gst.PadProbeReturn.OK
        buffer = info.get_buffer()
        pipeline = self.pipeline
        if buffer is not None and pipeline is not None and self.caps is not None:
            if buffer.pts != Gst.CLOCK_TIME_NONE:
                if not self.audio_ready.is_set():
                    self.audio_ready.set()
                self.handoff.offer(buffer, self.caps, pipeline.get_base_time() + buffer.pts)
        return Gst.PadProbeReturn.OK

    def refresh_latency(self):
        pipeline = self.pipeline
        if pipeline is None:
            return
        for name in ("audio_timeline", "video_timeline"):
            element = pipeline.get_by_name(name)
            if element is None or element.get_static_pad("src").get_current_caps() is None:
                return
        upstream = 0
        for name in ("audio_timeline", "video_timeline"):
            element = pipeline.get_by_name(name)
            query = Gst.Query.new_latency()
            if element is not None and element.get_static_pad("sink").peer_query(query):
                live, minimum, _maximum = query.parse_latency()
                if live and minimum != Gst.CLOCK_TIME_NONE:
                    upstream = max(upstream, minimum)
        # Query BEFORE delayed queues. Never relay the main pipeline's global
        # latency (which includes NDI delay) to the separate audio pipeline.
        output_minimum = 0
        output_started = False
        sinks = pipeline.iterate_sinks()
        while True:
            result, sink = sinks.next()
            if result != Gst.IteratorResult.OK:
                break
            if sink.find_property("stats") is not None:
                stats = sink.get_property("stats")
                if stats is not None and stats.has_field("rendered"):
                    output_started = output_started or int(stats.get_value("rendered")) > 0
            query = Gst.Query.new_latency()
            if sink.query(query):
                live, minimum, _maximum = query.parse_latency()
                if live and minimum != Gst.CLOCK_TIME_NONE:
                    output_minimum = max(output_minimum, minimum)
        # Aggregators can discover additional buffering requirements on their
        # first buffer. Keep automatic latency until that negotiation completes.
        if not self.latency_ready.is_set() and not output_started:
            return
        self.source_latency_ns = max(
            upstream + SCHEDULING_MARGIN_NS,
            output_minimum - self.delay_ns + 10 * MS,
        )
        pipeline.set_latency(self.source_latency_ns + self.delay_ns)
        pipeline.recalculate_latency()
        self.latency_ready.set()

    def close(self):
        self.handoff.deactivate()
        for pad, probe in self.probes:
            pad.remove_probe(probe)
        self.probes.clear()
        self.pipeline = None
        self.audio_ready.clear()
        self.latency_ready.clear()

    def snapshot(self):
        result = {"shared_base_latency_ms": self.source_latency_ns / MS,
                  "ready": self.latency_ready.is_set(),
                  "ndi_delay_ms": self.delay_ns / MS,
                  "ndi_queue_limit_ms": self.queue_ns / MS,
                  "audio_handoff": self.handoff.snapshot()}
        pipeline = self.pipeline
        if pipeline is not None:
            for name in ("ndi_audio_delay", "ndi_video_delay"):
                queue = pipeline.get_by_name(name)
                if queue is not None:
                    result[name] = {"time_ms": queue.get_property("current-level-time") / MS,
                                    "bytes": queue.get_property("current-level-bytes"),
                                    "max_buffers": queue.get_property("max-size-buffers"),
                                    "max_bytes": queue.get_property("max-size-bytes")}
        return result


class SharedAudioPipeline(GstPipelineBase):
    """An appsrc output with its own worker, base time, and bounded feeder."""

    def __init__(self, log_maxlen=300):
        super().__init__(log_maxlen)
        self.timing = None
        self.feeder = None
        self.feed_stop = threading.Event()

    def start_shared(self, description, timing, poll_cb=None):
        self.stop()
        if self.feeder is not None and self.feeder.is_alive():
            raise RuntimeError("The previous audio feeder is still stopping")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("The previous audio output is still stopping")
        self.timing = timing
        self.feed_stop = threading.Event()
        # _start_pipeline calls the base stop (not this override).
        self._start_pipeline(description, poll_cb=poll_cb)

    def _prepare_pipeline(self, pipeline):
        timing = self.timing
        pipeline.use_clock(timing.clock)
        pipeline.set_latency(timing.source_latency_ns)
        appsrc = pipeline.get_by_name("shared_audio")
        if appsrc is None:
            raise RuntimeError("Shared audio source is missing")
        timing.handoff.activate()
        self.feeder = threading.Thread(target=self._feed, args=(pipeline, appsrc, timing, self.feed_stop),
                                       name="teletool-pcm-feeder", daemon=True)
        self.feeder.start()

    def _feed(self, pipeline, appsrc, timing, cancelled):
        handoff = timing.handoff
        last_caps = None
        last_latency = None
        try:
            while not cancelled.is_set():
                # No preroll is needed: appsrc is live and the sink async=false.
                if pipeline.get_base_time() in (0, Gst.CLOCK_TIME_NONE):
                    cancelled.wait(0.005)
                    continue
                item = handoff.take()
                if item is None:
                    if handoff.eos:
                        appsrc.emit("end-of-stream")
                        return
                    handoff.ready.wait(0.05)
                    continue
                buffer, caps, absolute_pts, discont = item
                base = pipeline.get_base_time()
                if absolute_pts < base or absolute_pts + timing.source_latency_ns < timing.clock.get_time() - 20 * MS:
                    handoff.dropped += 1
                    handoff.discont = True
                    continue
                if last_caps is None or not last_caps.is_equal(caps):
                    appsrc.set_property("caps", caps)
                    last_caps = caps
                if last_latency != timing.source_latency_ns:
                    # Appsrc represents the original source, not a new live capture.
                    appsrc.set_property("min-latency", timing.source_latency_ns - SCHEDULING_MARGIN_NS)
                    pipeline.set_latency(timing.source_latency_ns)
                    pipeline.recalculate_latency()
                    last_latency = timing.source_latency_ns
                outgoing = retimestamp_pcm(buffer, absolute_pts - base, discont)
                result = appsrc.emit("push-buffer", outgoing)
                if result != Gst.FlowReturn.OK:
                    if not cancelled.is_set():
                        self._push_err(f"Shared audio output stopped accepting samples: {result.value_nick}")
                        self._request_stop()
                    return
        except Exception as exc:
            if not cancelled.is_set():
                self._push_err(f"Shared audio handoff failed: {exc}")
                self._request_stop()

    def stop(self):
        self.feed_stop.set()
        if self.timing is not None:
            self.timing.handoff.deactivate()
        super().stop()
        feeder = self.feeder
        if feeder is not None and feeder is not threading.current_thread():
            feeder.join(timeout=0.5)

    def _release_pipeline(self, pipeline):
        self.feed_stop.set()
        if self.timing is not None:
            self.timing.handoff.deactivate()
