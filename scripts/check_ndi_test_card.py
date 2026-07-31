#!/usr/bin/env python3
"""Validate the generated NDI test-card lifecycle and UI contract."""

import ast
import base64
import html
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
        'cfg.get("ndi_test_card_fps", 60)',
        'compositor name=testcardcompositor background=black max-threads=2',
        'rsvgdec ! imagefreeze is-live=true',
        '_write_test_card_marker()',
        'sink_1::width={marker_size}',
        'audiotestsrc is-live=true do-timestamp=true wave=ticks',
        'freq={tone_hz}',
        'tick-interval={tone_interval_ms * 1_000_000}',
        'multicast_enabled=bool(multicast_enabled_i)',
        'audio/x-raw,format=F32LE',
        'interaudiosink channel=teletool-test-card',
        'source_mode_i == "test_card"',
        "combiner.video",
        "combiner.audio",
    )

    motion_setup = source_for_function("gst_ndi.py", "_setup_test_card_motion", "GstNDIBridge")
    require(
        motion_setup,
        "test-card motion controller",
        'GstController.LFOControlSource()',
        'GstController.LFOWaveform.SINE',
        'source.set_property("frequency", 1.0)',
        '("ypos", 250_000_000, center_y - position_offset)',
        'DirectControlBinding.new',
        'pad.add_control_binding(binding)',
    )

    card_source = source_for_function("gst_ndi.py", "_test_card_background_svg")
    require(
        card_source,
        "test-card identity and timing reference",
        'class="host">{ip_address_i}</text>',
        'stroke="#ffd400" stroke-width="4"',
        'for frame in range(60)',
        'for frame in range(10, 60, 10)',
        '(1142, 332, "+10")',
        '(1142, 538, "+20")',
        '(960, 640, "30")',
        '(778, 538, "-20")',
        '(778, 332, "-10")',
        'x="0.5" y="0.5" width="1919" height="1079"',
        'stroke="#ffffff" stroke-width="1" shape-rendering="crispEdges"',
        'if multicast_enabled',
        'aria-label="Multicast Send"',
        '<title>Multicast Send</title>',
        'fill="#39e58c"',
        '>MOTION REFERENCE</text>',
        '>{hostname_i}</text>',
    )
    card_writer_source = source_for_function("gst_ndi.py", "_write_test_card_background")
    require(
        card_writer_source,
        "test-card multicast indicator wiring",
        "multicast_enabled: bool",
        "multicast_enabled=multicast_enabled",
    )
    for removed_label in ("NDI SOURCE", "TELETOOL CANDIDATE"):
        if removed_label in card_source:
            raise SystemExit(f"test-card background still contains removed label: {removed_label}")
    for split_frame_label in ('(995, 640, "+30")', '(925, 640, "-30")'):
        if split_frame_label in card_source:
            raise SystemExit(f"test-card clock still contains split frame label: {split_frame_label}")
    if 'for frame in range(5, 60, 5)' in card_source:
        raise SystemExit("test-card clock still contains full-length five-frame spokes")
    if "multicastLabel" in card_source:
        raise SystemExit("test-card multicast indicator still contains a visible text label")

    gst_source = (ROOT / "gst_ndi.py").read_text(encoding="utf-8")
    gst_tree = ast.parse(gst_source, filename="gst_ndi.py")
    card_node = next(
        node
        for node in gst_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_test_card_background_svg"
    )
    namespace = {"base64": base64, "html": html, "Path": Path}
    card_module = ast.fix_missing_locations(ast.Module(body=[card_node], type_ignores=[]))
    exec(compile(card_module, "gst_ndi.py", "exec"), namespace)
    render_card = namespace["_test_card_background_svg"]
    render_args = (1920, 1080, "test-host", "192.0.2.10", ROOT / "missing-logo.png")
    multicast_card = render_card(*render_args, multicast_enabled=True)
    unicast_card = render_card(*render_args, multicast_enabled=False)
    for marker in ('aria-label="Multicast Send"', "<title>Multicast Send</title>", "#39e58c"):
        if marker not in multicast_card:
            raise SystemExit(f"multicast test card is missing indicator marker: {marker}")
        if marker in unicast_card:
            raise SystemExit(f"unicast test card unexpectedly contains indicator marker: {marker}")

    lineout_pipeline = source_for_function("gst_ndi.py", "_build_lineout_pipeline_desc", "GstNDIBridge")
    require(
        lineout_pipeline,
        "test-card ALSA route",
        'source_mode == "test_card"',
        'interaudiosrc channel=teletool-test-card',
        'alsasink name=lineoutsink',
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
        'class="testCardSwitch"',
        'jpost("/api/test-card/start"',
        'jpost("/api/test-card/stop"',
        'ndiSourceMode === "test_card"',
        'sourceMode === "test_card" ? "TEST CARD"',
    )

    common_css = (ROOT / "static" / "common.css").read_text(encoding="utf-8")
    require(
        common_css,
        "NDI test-card switch styling",
        ".testCardSwitch::after",
        ".testCardToggle input:checked + .testCardSwitch",
        ".testCardToggle input:focus-visible + .testCardSwitch",
    )

    audio_html = (ROOT / "static" / "audio.html").read_text(encoding="utf-8")
    require(
        audio_html,
        "Audio page test-card state",
        "st.ndi_source_mode",
        'ndiSourceMode === "test_card"',
        "1 kHz alignment pulse ready",
        "1 kHz alignment pulse running",
    )

    print("NDI test card lifecycle checks passed")


if __name__ == "__main__":
    main()
