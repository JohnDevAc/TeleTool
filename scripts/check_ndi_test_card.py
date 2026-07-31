#!/usr/bin/env python3
"""Validate the generated NDI test-card lifecycle and UI contract."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source_for_function(path: str, function_name: str, class_name: str = "") -> str:
    source = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)
    nodes = tree.body
    if class_name:
        owner = next(
            (node for node in nodes if isinstance(node, ast.ClassDef) and node.name == class_name),
            None,
        )
        if owner is None:
            raise SystemExit(f"{path}: missing class {class_name}")
        nodes = owner.body
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_source_segment(source, node) or ast.unparse(node)
    raise SystemExit(f"{path}: missing function {function_name}")


def require(source: str, label: str, *needles: str) -> None:
    for needle in needles:
        if needle not in source:
            raise SystemExit(f"{label}: missing {needle!r}")


def main() -> None:
    bridge_start = source_for_function("gst_ndi.py", "start_with_delay", "GstNDIBridge")
    require(
        bridge_start,
        "test-card pipeline",
        'source_mode_i not in {"tv", "test_card"}',
        'videotestsrc is-live=true do-timestamp=true pattern=smpte',
        'audiotestsrc is-live=true do-timestamp=true wave=silence',
        'source_mode_i == "test_card"',
        "combiner.video",
        "combiner.audio",
    )

    lineout_start = source_for_function("gst_ndi.py", "lineout_start", "GstNDIBridge")
    require(
        lineout_start,
        "test-card audio guard",
        'source_mode != "tv"',
        "Audio output is unavailable while the NDI test card is running",
    )

    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    require(
        app_source,
        "test-card API",
        '@app.post("/api/test-card/start")',
        '@app.post("/api/test-card/stop")',
        'source_mode="test_card"',
        "Stop the active NDI stream before starting the test card",
        "Stop the NDI test card before starting the TV stream",
    )

    index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    require(
        index_html,
        "NDI test-card controls",
        'id="testCardEnabled"',
        'jpost("/api/test-card/start"',
        'jpost("/api/test-card/stop"',
        'ndiSourceMode === "test_card"',
        'sourceMode === "test_card" ? "TEST CARD"',
    )

    audio_html = (ROOT / "static" / "audio.html").read_text(encoding="utf-8")
    require(
        audio_html,
        "Audio page test-card state",
        "st.ndi_source_mode",
        'ndiSourceMode === "test_card"',
        "Audio is unavailable while the test card is running",
    )

    print("NDI test card lifecycle checks passed")


if __name__ == "__main__":
    main()
