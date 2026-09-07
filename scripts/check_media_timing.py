#!/usr/bin/env python3
"""Measure shared output timing and failure isolation with real Gst buffers."""

import faulthandler
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except (ImportError, ValueError):
    print("SKIP real media timing checks: GStreamer/PyGObject is required.")
    sys.exit(0)

from gst_base import GstPipelineBase
from gst_timing import AudioHandoff, MediaTiming, SharedAudioPipeline, audio_output_chain, video_output_chain, retimestamp_pcm

Gst.init(None)
MS = 1_000_000


class TimingPipeline(GstPipelineBase):
    def __init__(self, delay, source=None):
        super().__init__()
        self.timing = MediaTiming(delay * MS, (delay + 300) * MS, 48000, 2)
        self.samples = {"video": [], "audio": [], "local": []}
        self.source = source

    def record(self, output, pipeline):
        def handoff(sink, buffer, pad):
            # A fakesink handoff occurs AFTER GstBaseSink clock synchronization.
            pts = pipeline.get_base_time() + buffer.pts
            self.samples[output].append((pts, self.timing.clock.get_time()))
        return handoff

    def _prepare_pipeline(self, pipeline):
        self.timing.prepare(pipeline)
        for name in ("video", "audio"):
            pipeline.get_by_name(name).connect("handoff", self.record(name, pipeline))

    def _release_pipeline(self, pipeline):
        self.timing.close()

    def _on_bus_message_extra(self, message):
        if message.type == Gst.MessageType.LATENCY:
            self.timing.refresh_latency()
        return True

    def start(self):
        sinks = {name: f"fakesink name={name} sync=true async=false signal-handoffs=true" for name in ("video", "audio")}
        source = self.source or (
            "videotestsrc is-live=true ! video/x-raw,width=320,height=180,framerate=60/1 "
            "! identity name=v silent=true signal-handoffs=false "
            "audiotestsrc is-live=true name=a wave=ticks samplesperbuffer=480 "
        )
        desc = source + video_output_chain(
            "d." if self.source else "v.", "UYVY", self.timing.delay_ns, self.timing.queue_ns,
            progressive=not bool(self.source), destination=sinks["video"],
        ) + audio_output_chain(
            "d." if self.source else "a.", 48000, 2, self.timing.delay_ns, self.timing.queue_ns, destination=sinks["audio"],
        )
        self._start_pipeline(desc, poll_cb=lambda: self.timing.refresh_latency() or True)
        self._wait_until_playing(4)
        assert self.timing.latency_ready.wait(3)
        caps = self._pipeline.get_by_name("video_timeline").get_static_pad("src").get_current_caps()
        assert caps.get_structure(0).get_string("interlace-mode") == "progressive", caps.to_string()


class LocalOutput(SharedAudioPipeline):
    def __init__(self, owner, stall=None):
        super().__init__()
        self.owner = owner
        self.stall = stall

    def _prepare_pipeline(self, pipeline):
        super()._prepare_pipeline(pipeline)
        sink = pipeline.get_by_name("local")
        if self.stall:
            sink.connect("handoff", lambda *args: self.stall.wait(20))
        else:
            sink.connect("handoff", self.owner.record("local", pipeline))


LOCAL = (
    "appsrc name=shared_audio is-live=true format=time block=false do-timestamp=false "
    "max-bytes=76800 max-time=200000000 max-buffers=64 leaky-type=downstream "
    "! fakesink name=local sync=true async=false signal-handoffs=true"
)


def measure(delay, source=None):
    main = TimingPipeline(delay, source)
    local = LocalOutput(main)
    try:
        main.start()
        # A late attachment exposes accidental mutation of the primary buffer PTS.
        time.sleep(0.4)
        local.start_shared(LOCAL, main.timing)
        local._wait_until_playing(3)
        time.sleep(2.2)
        assert main._base_status_fields()["last_error"] is None
        assert local._base_status_fields()["last_error"] is None
        values = {}
        for name, samples in main.samples.items():
            assert len(samples) >= 30, (name, len(samples), main._base_status_fields())
            # Exclude start-up while latency is being negotiated.
            values[name] = statistics.median((wall - pts) / MS for pts, wall in samples[-30:])
        offset = values["video"] - values["local"]
        assert abs(offset - delay) < 12, (delay, values)
        assert abs(values["video"] - values["audio"]) < 5, values
        assert main.timing.snapshot()["ndi_video_delay"]["time_ms"] <= (delay + 300 + 20)
        local.stop()
        count = len(main.samples["video"])
        time.sleep(0.15)
        assert len(main.samples["video"]) > count
        local.start_shared(LOCAL, main.timing)
        local._wait_until_playing(3)
        time.sleep(0.15)
        print({"source": "decoded A/V" if source else "generated A/V", "delay_ms": delay,
               "observed_ms": values, "video_minus_local_ms": offset}, flush=True)
        return offset
    finally:
        local.stop()
        main.stop()
        assert local.feeder is None or not local.feeder.is_alive()


def test_stalled_sink():
    main = TimingPipeline(100)
    release = threading.Event()
    local = LocalOutput(main, release)
    try:
        main.start()
        local.start_shared(LOCAL, main.timing)
        local._wait_until_playing(3)
        count = len(main.samples["video"])
        time.sleep(0.7)
        assert len(main.samples["video"]) - count > 25
        snapshot = main.timing.handoff.snapshot()
        assert snapshot["queued_bytes"] <= snapshot["max_bytes"]
        assert local._pipeline.get_by_name("shared_audio").get_property("current-level-bytes") <= 76800
        start = time.monotonic()
        local.stop()
        assert time.monotonic() - start < 2.7
        count = len(main.samples["video"])
        time.sleep(0.2)
        assert len(main.samples["video"]) > count
        try:
            local.start_shared(LOCAL, main.timing)
        except RuntimeError as exc:
            assert "still stopping" in str(exc)
        else:
            raise AssertionError("An audio worker was replaced before teardown finished")
    finally:
        release.set()
        local.stop()
        main.stop()
    print("Stalled audio sink stays bounded and cannot stop NDI", flush=True)


def test_bounds():
    original = Gst.Buffer.new_allocate(None, 128, None)
    original.pts = 123 * MS
    original.dts = 122 * MS
    original.unset_flags(Gst.BufferFlags.DISCONT)
    outgoing = retimestamp_pcm(original, 7 * MS, True)
    assert original.pts == 123 * MS and original.dts == 122 * MS
    assert not original.has_flags(Gst.BufferFlags.DISCONT)
    assert outgoing.pts == 7 * MS and outgoing.has_flags(Gst.BufferFlags.DISCONT)
    handoff = AudioHandoff(1024, 100 * MS)
    handoff.activate()
    caps = Gst.Caps.from_string("audio/x-raw,format=F32LE,rate=48000,channels=2")
    for index in range(1000):
        handoff.offer(Gst.Buffer.new_allocate(None, 512, None), caps, index * MS)
    assert handoff.snapshot()["queued_bytes"] == 1024
    assert handoff.dropped == 998
    handoff.lock.acquire()
    try:
        handoff.offer(Gst.Buffer.new_allocate(None, 512, None), caps, 1001 * MS)
    finally:
        handoff.lock.release()
    assert handoff.dropped == 999
    handoff.deactivate()
    assert handoff.snapshot()["queued_bytes"] == 0


def test_encoded_timeline():
    with tempfile.TemporaryDirectory(prefix="teletool-av-fixture-") as directory:
        path = Path(directory) / "synchronized.ogg"
        encoder = Gst.parse_launch(
            f'oggmux name=m ! filesink location="{path.as_posix()}" '
            'videotestsrc num-buffers=360 pattern=ball ! '
            'video/x-raw,width=320,height=180,framerate=60/1 ! theoraenc ! queue ! m. '
            'audiotestsrc num-buffers=600 wave=ticks samplesperbuffer=480 ! '
            'audio/x-raw,rate=48000,channels=2 ! audioconvert ! vorbisenc ! queue ! m. '
        )
        try:
            encoder.set_state(Gst.State.PLAYING)
            message = encoder.get_bus().timed_pop_filtered(15 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            assert message is not None and message.type == Gst.MessageType.EOS, message
        finally:
            encoder.set_state(Gst.State.NULL)
        source = f'uridecodebin uri="{path.as_uri()}" name=d '
        low, high = measure(100, source), measure(500, source)
        assert abs((high - low) - 400) < 12


def test_eos():
    main = TimingPipeline(100)
    local = LocalOutput(main)
    try:
        main.start()
        local.start_shared(LOCAL, main.timing)
        time.sleep(0.2)
        main._pipeline.send_event(Gst.Event.new_eos())
        main._thread.join(3)
        local._thread.join(3)
        assert not main._thread.is_alive() and not local._thread.is_alive()
    finally:
        local.stop()
        main.stop()


def test_mixed_interlace_caps():
    main = TimingPipeline(100)
    description = video_output_chain(
        "videotestsrc is-live=true ! video/x-raw,width=320,height=180,framerate=60/1 "
        "! capssetter caps=video/x-raw,interlace-mode=mixed replace=false",
        "UYVY", 100 * MS, 400 * MS,
        destination="fakesink name=video sync=true async=false signal-handoffs=true",
    ) + audio_output_chain(
        "audiotestsrc is-live=true", 48000, 2, 100 * MS, 400 * MS,
        destination="fakesink name=audio sync=true async=false signal-handoffs=true",
    )
    try:
        main._start_pipeline(description)
        main._wait_until_playing(3)
        caps = main._pipeline.get_by_name("video_timeline").get_static_pad("src").get_current_caps()
        assert caps.get_structure(0).get_string("interlace-mode") == "interleaved", caps.to_string()
    finally:
        main.stop()


if __name__ == "__main__":
    faulthandler.dump_traceback_later(60, exit=True)
    try:
        test_bounds()
        a, b = measure(100), measure(500)
        assert abs((b - a) - 400) < 12
        test_encoded_timeline()
        test_stalled_sink()
        test_eos()
        test_mixed_interlace_caps()
    finally:
        faulthandler.cancel_dump_traceback_later()
    print("Real shared-media timing and isolation checks passed.")
