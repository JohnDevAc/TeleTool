#!/usr/bin/env python3
"""Validate that secondary audio sinks cannot outlive an Audio session."""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
import threading
import time


ROOT = Path(__file__).resolve().parents[1]


def method_source(path: str, class_name: str, method_name: str) -> str:
    source = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)
    if not class_name:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
                return ast.unparse(node)
        raise SystemExit(f"{path}: missing {method_name}")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    return ast.unparse(item)
    raise SystemExit(f"{path}: missing {class_name}.{method_name}")


def require(source: str, label: str, *needles: str) -> None:
    source = source.replace('"', "'")
    for needle in needles:
        needle = needle.replace('"', "'")
        if needle not in source:
            raise SystemExit(f"{label}: missing lifecycle operation: {needle}")


init = method_source("gst_ndi.py", "GstNDIBridge", "__init__")
require(
    init,
    "audio pipeline ownership",
    "self._lineout_pipeline = SharedAudioPipeline(log_maxlen=300)",
)

pipeline_desc = method_source("gst_ndi.py", "GstNDIBridge", "_build_lineout_pipeline_desc")
require(
    pipeline_desc,
    "isolated audio-only pipeline",
    "appsrc name=shared_audio",
    "block=false",
    "leaky-type=downstream",
    "audioconvert ! audioresample",
    "alsasink name=lineoutsink",
)
if "video/" in pipeline_desc:
    raise SystemExit("isolated audio pipeline must not decode video")
for forbidden in ("uridecodebin", "interaudiosrc", "uri="):
    if forbidden in pipeline_desc:
        raise SystemExit(f"separate audio must use the shared decoded timeline, not {forbidden}")

start = method_source("gst_ndi.py", "GstNDIBridge", "lineout_start")
require(
    start,
    "audio start",
    "self._lineout_pipeline.start_shared(",
    "self._lineout_pipeline._wait_until_playing",
    "self._lineout_pipeline.stop()",
    "self._inferno_clock_status(force=True)",
    "raise ValueError(error) from e",
)

stop = method_source("gst_ndi.py", "GstNDIBridge", "lineout_stop")
require(
    stop,
    "audio stop",
    "self._lineout_pipeline.stop()",
    "self._lineout_pipeline._clear_status()",
)

ndi_start = method_source("gst_ndi.py", "GstNDIBridge", "start_with_delay")
for forbidden in ("lineoutsink", "lineoutvalve", "lineoutvolume", "tee name=atee"):
    if forbidden in ndi_start:
        raise SystemExit(f"primary NDI pipeline still contains secondary audio element: {forbidden}")

sync_call = method_source("gst_base.py", "GstPipelineBase", "_call_in_gst_context_sync")
require(
    sync_call,
    "GStreamer callback",
    "gst_thread is threading.current_thread()",
    "return fn()",
)

wait_call = method_source("gst_base.py", "GstPipelineBase", "_wait_until_playing")
require(
    wait_call,
    "audio pipeline readiness",
    "pipeline.get_state(0)",
    "state == Gst.State.PLAYING",
    "raise RuntimeError(last_error)",
)

restore = method_source("app.py", "", "_restore_desired_lineout")
require(
    restore,
    "audio supervisor failure handoff",
    'current.get("last_error")',
    'NDI_SUPERVISOR_STATE["lineout_desired"] = False',
    'NDI_SUPERVISOR_STATE["lineout_request"] = None',
)

system_html = (ROOT / "static" / "system.html").read_text(encoding="utf-8")
if 'window.location.replace("/")' not in system_html:
    raise SystemExit("static/system.html: successful updates must return to the main UI")


def check_sink_timing_policy():
    """Exercise a real start with saved settings; replace only hardware calls."""
    scope = {"Any": Any, "Dict": Dict, "Optional": Optional, "time": time}
    exec(method_source("gst_ndi.py", "", "_gst_quote"), scope)
    exec(pipeline_desc, scope)
    exec(start, scope)
    for kind in ("inferno", "usb"):
        for configured_sync in (False, True):
            selected = {"id": "alsa:test", "device": "test", "sink": "alsasink", "kind": kind}
            launched = []
            ready = threading.Event()
            ready.set()
            bridge = SimpleNamespace(
                _lock=threading.RLock(), _source_mode="test_card", _input_url="test-card://local",
                _cfg={"lineout_sink_sync": configured_sync},
                _base_status_fields=lambda **kwargs: {"running": True},
                _resolve_audio_output_device=lambda device: selected,
                _inferno_clock_status=lambda **kwargs: {"ready": True},
                _media_timing=SimpleNamespace(pipeline=object(), audio_ready=ready, latency_ready=ready),
                _lineout_pipeline=SimpleNamespace(
                    start_shared=lambda description, *args, **kwargs: launched.append(description),
                    _wait_until_playing=lambda **kwargs: None,
                    stop=lambda: None,
                ),
                _lineout_log_push=lambda message: None,
            )
            bridge._build_lineout_pipeline_desc = lambda **kwargs: scope["_build_lineout_pipeline_desc"](bridge, **kwargs)
            scope["lineout_start"](bridge, device_id=selected["id"])
            expected = configured_sync if kind == "usb" else False
            assert len(launched) == 1 and f" sync={str(expected).lower()}" in launched[0], launched
            assert bridge._lineout_sink_sync is expected
            assert bridge._lineout_enabled


check_sink_timing_policy()
print("Audio output lifecycle and update redirect tests passed.")
