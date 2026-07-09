import threading
import time
from collections import deque
from typing import Callable, Deque, Optional

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib  # type: ignore

Gst.init(None)


class GstPipelineBase:
    """Common lifecycle + logging for the bridge pipelines.

    Subclasses typically:
    - fill in metadata fields in their own start()
    - call _start_pipeline(pipeline_desc, poll_cb=...)
    - implement their own status() using _base_status_fields()
    """

    def __init__(self, log_maxlen: int = 400):
        self._lock = threading.Lock()

        self._loop: Optional[GLib.MainLoop] = None
        self._context: Optional[GLib.MainContext] = None
        self._thread: Optional[threading.Thread] = None
        self._pipeline: Optional[Gst.Pipeline] = None
        self._bus_watch_id: Optional[int] = None
        self._poll_id: Optional[int] = None

        self._log_full: Deque[str] = deque(maxlen=log_maxlen)
        # Tail log is used for frequent UI polling to avoid copying the full log deque.
        self._log_tail: Deque[str] = deque(maxlen=60)

        self._pipeline_state: str = "NULL"
        self._last_error: Optional[str] = None
        self._last_warning: Optional[str] = None

    # ---------- logging helpers ----------

    def _push_log(self, msg: str):
        with self._lock:
            line = f"{time.strftime('%H:%M:%S')} {msg}"
            self._log_full.append(line)
            self._log_tail.append(line)

    def _push_err(self, msg: str):
        with self._lock:
            self._last_error = msg
            line = f"{time.strftime('%H:%M:%S')} {msg}"
            self._log_full.append(line)
            self._log_tail.append(line)

    def _push_warn(self, msg: str):
        with self._lock:
            self._last_warning = msg
            line = f"{time.strftime('%H:%M:%S')} {msg}"
            self._log_full.append(line)
            self._log_tail.append(line)

    def _suppress_gst_warning(self, message: str, debug: Optional[str]) -> bool:
        text = f"{message} {debug or ''}".upper()
        # MPEG-TS continuity warnings often occur during channel tune/start while
        # Tvheadend and tsdemux align to the new stream. They are noisy but not
        # actionable if the pipeline reaches PLAYING and renders normally.
        return "CONTINUITY: MISMATCH PACKET" in text and "TSDEMUX" in text

    def _set_pipeline_state(self, state_name: str):
        with self._lock:
            self._pipeline_state = state_name

    def _base_status_fields(self, include_log: bool = True):
        with self._lock:
            # Consider the pipeline "running" once we have a pipeline object and
            # we've progressed beyond NULL/READY.
            #
            # Why: on some installs the STATE_CHANGED message for the top-level
            # pipeline isn't always observed (GI wrapper differences), which can
            # leave _pipeline_state stuck at NULL even though the pipeline is
            # PLAYING. The UI uses `running` to decide whether to show live stats.
            # If we have a pipeline object but never observed STATE_CHANGED on the
            # top-level pipeline (some GI builds), keep the UI sensible.
            state_for_ui = self._pipeline_state
            if self._pipeline is not None and state_for_ui in ("NULL", "READY"):
                state_for_ui = "PLAYING"

            # Treat PLAYING/PAUSED as "running" for UI + secondary-output gating.
            running = self._pipeline is not None and state_for_ui in ("PAUSED", "PLAYING")

            d = {
                "running": running,
                "pipeline_state": state_for_ui,
                "last_error": self._last_error,
                "last_warning": self._last_warning,
            }
            if include_log:
                # Avoid copying the full log deque on every poll.
                d["last_log"] = list(self._log_tail)
            return d


    # ---------- lifecycle ----------

    def _call_in_gst_context(self, fn) -> bool:
        """Schedule fn to run in the GStreamer GLib context thread.

        Returns True if the call was scheduled, False if no pipeline/context is running.
        """
        with self._lock:
            ctx = self._context
        if ctx is None:
            return False

        def _cb(_data=None):
            try:
                fn()
            except Exception as e:
                self._push_warn(f"GST context callback failed: {e}")
            return False

        try:
            src = GLib.idle_source_new()
            src.set_callback(_cb, None)
            src.attach(ctx)
            return True
        except Exception:
            return False

    def _call_in_gst_context_sync(self, fn, timeout_s: float = 2.0):
        """Run fn in the GStreamer context and propagate its exception/result.

        This is used for configuration changes where the caller must know whether
        the change actually succeeded before advertising traffic or returning API
        success.
        """
        with self._lock:
            ctx = self._context
        if ctx is None:
            raise RuntimeError("GStreamer context is not running")
        done = threading.Event()
        box = {"ok": False, "result": None, "error": None}

        def _cb(_data=None):
            try:
                box["result"] = fn()
                box["ok"] = True
            except Exception as e:
                box["error"] = e
                self._push_warn(f"GST context callback failed: {e}")
            finally:
                done.set()
            return False

        src = GLib.idle_source_new()
        src.set_callback(_cb, None)
        src.attach(ctx)
        if not done.wait(timeout=max(0.1, float(timeout_s))):
            raise RuntimeError("Timed out waiting for GStreamer context callback")
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    def _start_pipeline(self, pipeline_desc: str, poll_cb: Optional[Callable[[], bool]] = None):
        GstPipelineBase.stop(self)

        with self._lock:
            self._log_full.clear()
            self._log_tail.clear()
            self._pipeline_state = "NULL"
            self._last_error = None
            self._last_warning = None

        self._thread = threading.Thread(target=self._run_gst_thread, args=(pipeline_desc, poll_cb), daemon=True)
        self._thread.start()

    def stop(self):
        with self._lock:
            pipeline = self._pipeline
            loop = self._loop
            context = self._context
            bus_watch_id = self._bus_watch_id
            poll_id = self._poll_id
            thread = self._thread

        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass

        if bus_watch_id is not None and pipeline is not None:
            try:
                bus = pipeline.get_bus()
                bus.disconnect(bus_watch_id)
            except Exception:
                pass

        if poll_id is not None:
            try:
                if context is None or context.find_source_by_id(poll_id) is not None:
                    GLib.source_remove(poll_id)
            except Exception:
                pass

        if loop is not None and loop.is_running():
            try:
                loop.quit()
            except Exception as e:
                self._push_warn(f"Failed to quit GStreamer loop: {e}")

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            try:
                thread.join(timeout=2.0)
            except Exception as e:
                self._push_warn(f"Failed to join GStreamer thread: {e}")

        with self._lock:
            self._pipeline = None
            self._loop = None
            self._context = None
            self._bus_watch_id = None
            self._poll_id = None
            self._thread = None
            self._pipeline_state = "NULL"

    # ---------- GStreamer thread ----------

    def _run_gst_thread(self, pipeline_desc: str, poll_cb: Optional[Callable[[], bool]]):
        try:
            pipeline = Gst.parse_launch(pipeline_desc)
            if not isinstance(pipeline, Gst.Pipeline):
                raise RuntimeError("Pipeline is not a Gst.Pipeline")
        except Exception as e:
            self._push_err(f"Pipeline build failed: {e}")
            return

        # Run the pipeline inside its own GLib MainContext so other threads can
        # safely schedule work (element property changes, etc.) into this loop.
        context = GLib.MainContext()
        context.push_thread_default()
        loop = GLib.MainLoop.new(context, False)
        bus = pipeline.get_bus()
        bus.add_signal_watch()

        with self._lock:
            self._pipeline = pipeline
            self._loop = loop
            self._context = context

        self._bus_watch_id = bus.connect("message", self._on_bus_message)

        if poll_cb is not None:
            # poll_cb must return True to keep polling
            poll_source = GLib.timeout_source_new_seconds(1)
            poll_source.set_callback(lambda _data=None: bool(poll_cb()), None)
            self._poll_id = poll_source.attach(context)

        try:
            pipeline.set_state(Gst.State.PLAYING)
        except Exception as e:
            self._push_err(f"Failed to set PLAYING: {e}")
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            with self._lock:
                if self._pipeline is pipeline:
                    self._pipeline = None
                    self._loop = None
                    self._context = None
                    self._bus_watch_id = None
                    self._poll_id = None
                    self._pipeline_state = "NULL"
            return

        try:
            loop.run()
        finally:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            try:
                bus.remove_signal_watch()
            except Exception:
                pass

            try:
                context.pop_thread_default()
            except Exception:
                pass

            with self._lock:
                if self._pipeline is pipeline:
                    self._pipeline = None
                    self._loop = None
                    self._context = None
                    self._bus_watch_id = None
                    self._poll_id = None
                    self._pipeline_state = "NULL"

    def _on_bus_message(self, _bus: Gst.Bus, msg: Gst.Message):
        t = msg.type

        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            self._push_err(f"ERROR: {err.message}" + (f" | {dbg}" if dbg else ""))
            self.stop()

        elif t == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            if self._suppress_gst_warning(err.message, dbg):
                return
            self._push_warn(f"WARNING: {err.message}" + (f" | {dbg}" if dbg else ""))

        elif t == Gst.MessageType.EOS:
            self._push_log("EOS")
            self.stop()

        elif t == Gst.MessageType.STATE_CHANGED:
            # We only track the top-level pipeline state.
            # Don't rely on `isinstance(msg.src, Gst.Pipeline)` — with GI
            # bindings this can be false even when the src is the pipeline.
            with self._lock:
                pipeline = self._pipeline
            if pipeline is not None:
                try:
                    is_pipeline = (msg.src == pipeline) or (
                        hasattr(msg.src, "get_name") and msg.src.get_name() == pipeline.get_name()
                    )
                except Exception:
                    is_pipeline = False

                if is_pipeline:
                    old, new, _pending = msg.parse_state_changed()
                    self._set_pipeline_state(Gst.Element.state_get_name(new))
                    self._push_log(
                        f"STATE: {Gst.Element.state_get_name(old)} -> {Gst.Element.state_get_name(new)}"
                    )

        # allow subclasses to observe other messages without copy-paste
        try:
            return bool(self._on_bus_message_extra(msg))
        except Exception:
            return True

    def _on_bus_message_extra(self, msg: Gst.Message) -> bool:
        """Subclass hook. Return True to keep watch."""
        return True
