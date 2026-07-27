#!/usr/bin/env python3
"""Validate that secondary audio sinks cannot outlive an Audio session."""

import ast
from pathlib import Path


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
    "self._lineout_pipeline = GstPipelineBase(log_maxlen=300)",
)

pipeline_desc = method_source("gst_ndi.py", "GstNDIBridge", "_build_lineout_pipeline_desc")
require(
    pipeline_desc,
    "isolated audio-only pipeline",
    "caps=audio/x-raw",
    "lineoutdecode. ! queue",
    "audioconvert ! audioresample",
    "alsasink name=lineoutsink",
)
if "video/" in pipeline_desc:
    raise SystemExit("isolated audio pipeline must not decode video")

start = method_source("gst_ndi.py", "GstNDIBridge", "lineout_start")
require(
    start,
    "audio start",
    "self._lineout_pipeline._start_pipeline(pipeline_desc)",
    "self._lineout_pipeline._wait_until_playing",
    "self._lineout_pipeline.stop()",
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

print("Audio output lifecycle and update redirect tests passed.")
