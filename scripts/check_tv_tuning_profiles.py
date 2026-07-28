#!/usr/bin/env python3
"""Validate the managed UK DVB-T/T2 tuning profile without runtime imports."""

import ast
import collections
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
AUTO_KEY = "teletool/uk-auto-dvbt-dvbt2"


def load_function(path: Path, function_name: str, globals_dict: Dict[str, Any]):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    ]
    if len(functions) != 1:
        raise AssertionError(f"Expected one {function_name} function in {path}, found {len(functions)}")
    namespace = dict(globals_dict)
    exec(compile(ast.Module(body=[functions[0]], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[function_name]


build_muxes = load_function(
    ROOT / "tvh.py",
    "_load_uk_auto_dvbt2_muxes",
    {"Any": Any, "Dict": Dict, "List": List},
)
muxes = build_muxes(None)
systems = collections.Counter(mux["delsys"] for mux in muxes)

assert len(muxes) == 112, f"Expected 112 UK auto muxes, found {len(muxes)}"
assert systems == {"DVB-T": 28, "DVB-T2": 84}, systems
assert min(mux["frequency"] for mux in muxes) == 473_833_000
assert max(mux["frequency"] for mux in muxes) == 690_167_000
assert len({(mux["frequency"], mux["delsys"]) for mux in muxes}) == 112
assert {
    mux["frequency"]
    for mux in muxes
    if mux["delsys"] == "DVB-T2" and 545_000_000 <= mux["frequency"] <= 547_000_000
} == {545_833_000, 546_000_000, 546_167_000}

coerce_int = load_function(
    ROOT / "app.py",
    "_coerce_int",
    {"Any": Any, "Optional": Optional},
)
mux_is_active = load_function(
    ROOT / "app.py",
    "_mux_is_active",
    {"Any": Any, "Dict": Dict, "_coerce_int": coerce_int},
)
scan_mux_summary = load_function(
    ROOT / "app.py",
    "_scan_mux_summary",
    {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "_coerce_int": coerce_int,
        "_mux_is_active": mux_is_active,
    },
)
dvbt2_muxes_with_services = load_function(
    ROOT / "app.py",
    "_dvbt2_muxes_with_services",
    {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "_coerce_int": coerce_int,
        "re": re,
    },
)
scan_summary = scan_mux_summary([
    {"uuid": "complete", "scan_state": 0, "num_svc": 7},
    {"uuid": "active", "scan_state": 1, "num_svc": 5},
])
assert scan_summary == {
    "muxes": 2,
    "active": 1,
    "complete": 1,
    "services": 12,
}
assert dvbt2_muxes_with_services([
    {"delsys": "DVB-T", "num_svc": 8},
    {"delsys": "DVB-T2", "num_svc": 6},
    {"delivery_system": "DVBT2", "num_svc": 0},
]) == 1

is_broadcast_av_service = load_function(
    ROOT / "app.py",
    "_is_broadcast_av_service",
    {
        "Any": Any,
        "Dict": Dict,
        "_DVB_BROADCAST_SERVICE_TYPES": {
            0x01, 0x02, 0x11, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B,
            0x1C, 0x1D, 0x1E, 0x1F, 0x80, 0x91, 0x96, 0xA0, 0xA4,
            0xA6, 0xA8, 0xD3,
        },
    },
)
component_retry_targets = load_function(
    ROOT / "app.py",
    "_component_retry_targets",
    {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "_is_broadcast_av_service": is_broadcast_av_service,
    },
)
discovered = [
    {
        "uuid": "verified-tv",
        "multiplex_uuid": "mux-sd",
        "svcname": "Ready TV",
        "dvb_servicetype": 1,
        "enabled": True,
    },
    {
        "uuid": "unverified-hd",
        "multiplex_uuid": "mux-hd",
        "svcname": "BBC ONE HD",
        "dvb_servicetype": 25,
        "enabled": True,
    },
    {
        "uuid": "data",
        "multiplex_uuid": "mux-data",
        "svcname": "Portal",
        "dvb_servicetype": 12,
        "enabled": True,
    },
    {
        "uuid": "disabled",
        "multiplex_uuid": "mux-hd",
        "svcname": "Disabled HD",
        "dvb_servicetype": 25,
        "enabled": False,
    },
    {
        "uuid": "verified-unnamed",
        "multiplex_uuid": "mux-hd",
        "sid": 17540,
        "svcname": "",
        "dvb_servicetype": 0,
        "enabled": True,
    },
]
assert component_retry_targets(
    discovered,
    [{"uuid": "verified-tv"}, {"uuid": "verified-unnamed"}],
) == {
    "mux-hd": ["BBC ONE HD", "Service SID 17540"],
}

preferred_scanfile = load_function(
    ROOT / "app.py",
    "_preferred_dvbt_scanfile",
    {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "TELETOOL_UK_AUTO_SCANFILE": AUTO_KEY,
        "re": re,
    },
)
regions = [
    {"key": "dvb-t/auto/dvb-t_auto-Defaul", "val": "--Generic--: auto-Default"},
    {"key": "dvb-t/uk/dvb-t_uk-CrystalPalac", "val": "United Kingdom: uk-CrystalPalace"},
    {"key": AUTO_KEY, "val": "Generic Auto Default (UK DVB-T/T2)"},
]
assert preferred_scanfile(regions, "") == AUTO_KEY
assert preferred_scanfile(regions, "dvb-t/auto/dvb-t_auto-Defaul") == AUTO_KEY
assert (
    preferred_scanfile(regions, "dvb-t/uk/dvb-t_uk-CrystalPalac")
    == "dvb-t/uk/dvb-t_uk-CrystalPalac"
)

tvh_tree = ast.parse((ROOT / "tvh.py").read_text(encoding="utf-8"))
map_services = next(
    node for node in ast.walk(tvh_tree) if isinstance(node, ast.FunctionDef) and node.name == "map_services"
)
merge_values = [
    value.value
    for node in ast.walk(map_services)
    if isinstance(node, ast.Dict)
    for key, value in zip(node.keys, node.values)
    if isinstance(key, ast.Constant)
    and key.value == "merge_same_name"
    and isinstance(value, ast.Constant)
]
assert merge_values == [True], "TV service mapping must merge duplicate channel names"

scan_muxes = load_function(
    ROOT / "tvh.py",
    "scan_muxes",
    {"List": List, "json": json},
)


class FakeTvh:
    def __init__(self):
        self.calls = []

    def _post_jsonish(self, path, *, data=None):
        self.calls.append((path, data))
        return {}


fake_tvh = FakeTvh()
scan_muxes(fake_tvh, ["mux-a", "mux-a", "", "mux-b"])
assert [json.loads(data["node"]) for _path, data in fake_tvh.calls] == [
    {"uuid": "mux-a", "scan_state": 3},
    {"uuid": "mux-b", "scan_state": 3},
]

ensure_scan_grace = load_function(
    ROOT / "tvh.py",
    "ensure_dvbt_scan_grace",
    {"List": List, "Dict": Dict, "json": json},
)


class FakeGraceTvh(FakeTvh):
    def list_linux_dvbt_frontends(self):
        return [
            {
                "uuid": "short",
                "text": "Short grace tuner",
                "params": [{"id": "grace_period", "value": 5}],
            },
            {
                "uuid": "custom",
                "text": "Custom grace tuner",
                "params": [{"id": "grace_period", "value": 30}],
            },
        ]


fake_grace_tvh = FakeGraceTvh()
grace_results = ensure_scan_grace(fake_grace_tvh, 20)
assert grace_results == [
    {
        "uuid": "short",
        "name": "Short grace tuner",
        "previous": 5,
        "current": 20,
        "changed": True,
    },
    {
        "uuid": "custom",
        "name": "Custom grace tuner",
        "previous": 30,
        "current": 30,
        "changed": False,
    },
]
assert [json.loads(data["node"]) for _path, data in fake_grace_tvh.calls] == [
    {"uuid": "short", "grace_period": 20},
]

index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
common_js = (ROOT / "static" / "common.js").read_text(encoding="utf-8")
assert "Generic Auto Legacy (DVB-T only)" in index_html
assert "teletool-uk-auto-dvbt-dvbt2" in index_html
profile_helper = index_html.index("function isTeleToolUkAuto(value)")
profile_preference = index_html.index("function preferredTvSetupRegion(options, selected)")
profile_loader = index_html.index("async function loadTvSetupRegions()")
assert profile_helper < profile_preference < profile_loader
assert "const isTeleToolUkAuto" not in index_html
assert 'id="tvSetupMuxCount"' in index_html
assert 'id="tvSetupServiceCount"' in index_html
assert "st.muxes_scanned" in index_html
assert "st.muxes_total" in index_html
assert "st.services_found" in index_html
assert "rfSignalStatsLabel(rf)" in index_html
assert "function rfCnrLabel(rf)" in common_js
assert "function rfSignalStatsLabel(rf)" in common_js
assert "| C/N ${rfCnrLabel(rf)}" in common_js

app_source = (ROOT / "app.py").read_text(encoding="utf-8")
assert 'muxes_scanned=summary["complete"]' in app_source
assert 'muxes_total=summary["muxes"]' in app_source
assert 'services_found=summary["services"]' in app_source
assert "No DVB-T2 multiplex locked" in app_source
assert '"cnr_db": cnr_db' in app_source
assert '"cnr_label": cnr_label' in app_source
assert "ensure_dvbt_scan_grace(scan_grace_s)" in app_source
assert "_wait_for_component_retries(" in app_source
assert "Service readiness after retry:" in app_source

configure_source = (ROOT / "packaging" / "debian" / "configure-tvheadend").read_text(encoding="utf-8")
assert 'frontend["grace_period"] = 20' in configure_source

print("UK DVB-T/T2 tuning profile checks passed.")
