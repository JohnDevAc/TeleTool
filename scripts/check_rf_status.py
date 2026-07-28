#!/usr/bin/env python3
"""Validate calibrated RF signal and carrier-to-noise conversion."""

import ast
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app.py"
FUNCTIONS = {
    "_rf_number",
    "_rf_percent",
    "_rf_kind",
    "_rf_kind_from_dbm",
    "_rf_text",
    "_rf_dbm_from_signal",
    "_rf_dbm_label",
    "_rf_scale_is_db",
    "_rf_scaled_db_value",
    "_rf_scaled_snr_text",
    "_rf_dbm_from_signal_scaled",
    "_rf_status_from_fields",
    "_coerce_int",
    "_rf_norm_ref",
    "_rf_freq_tokens",
    "_rf_input_matches_mux",
}

tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
nodes = [
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
]
assert {node.name for node in nodes} == FUNCTIONS

namespace = {
    "Any": Any,
    "Dict": Dict,
    "List": List,
    "Optional": Optional,
    "Tuple": Tuple,
    "re": re,
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
status_from_fields = namespace["_rf_status_from_fields"]

calibrated = status_from_fields(
    signal=-52_000,
    snr=24_500,
    signal_scale=2,
    snr_scale=2,
    mux_label="BBC B / 586.000 MHz",
    source="test",
)
assert calibrated["dbm"] == -52.0
assert calibrated["dbm_estimated"] is False
assert calibrated["dbm_label"] == "-52 dBm"
assert calibrated["cnr_db"] == 24.5
assert calibrated["cnr_label"] == "24.5 dB"
assert calibrated["kind"] == "good"

good = status_from_fields(
    signal=-60_000,
    snr=32_000,
    signal_scale="dBm",
    snr_scale="dB",
    source="test",
)
assert good["kind"] == "good"

raw = status_from_fields(
    signal=49_151,
    snr=45_000,
    signal_scale=1,
    snr_scale=1,
    source="test",
)
assert raw["dbm_estimated"] is True
assert raw["cnr_db"] is None
assert raw["cnr_label"] is None

matches_mux = namespace["_rf_input_matches_mux"]
assert matches_mux(
    {"stream": "586MHz in DVB-T Network"},
    {"uuid": "mux-586", "frequency": 586_000_000},
)
assert not matches_mux(
    {"stream": "586MHz in DVB-T Network"},
    {"uuid": "mux-650", "frequency": 650_000_000},
)

print("Calibrated dBm and C/N RF status conversion is valid.")
