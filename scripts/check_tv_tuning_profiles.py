#!/usr/bin/env python3
"""Validate the managed UK DVB-T/T2 tuning profile without runtime imports."""

import ast
import collections
import json
import re
from statistics import median
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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

centre_frequencies = load_function(
    ROOT / "tvh.py",
    "uk_auto_centre_frequencies",
    {"List": List},
)
nominal_muxes = load_function(
    ROOT / "tvh.py",
    "uk_auto_nominal_muxes",
    {"Any": Any, "Dict": Dict, "List": List},
)
offset_muxes = load_function(
    ROOT / "tvh.py",
    "uk_auto_offset_muxes",
    {"Any": Any, "Dict": Dict, "List": List, "Optional": Optional},
)


class FakeAutoProfile:
    def uk_auto_centre_frequencies(self):
        return centre_frequencies()

    def _load_uk_auto_dvbt2_muxes(self):
        return muxes


fake_auto_profile = FakeAutoProfile()
nominal = nominal_muxes(fake_auto_profile)
assert len(nominal) == 56
assert collections.Counter(mux["delsys"] for mux in nominal) == {
    "DVB-T": 28,
    "DVB-T2": 28,
}
offsets = offset_muxes(fake_auto_profile, [546_000_000])
assert len(offsets) == 54
assert all(mux["delsys"] == "DVB-T2" for mux in offsets)
assert not {
    mux["frequency"]
    for mux in offsets
    if 545_000_000 <= mux["frequency"] <= 547_000_000
}
negative_offsets = offset_muxes(fake_auto_profile, [546_000_000], [-167_000])
positive_offsets = offset_muxes(fake_auto_profile, [546_000_000], [167_000])
assert len(negative_offsets) == 27
assert len(positive_offsets) == 27
assert all((mux["frequency"] + 167_000) % 8_000_000 == 2_000_000 for mux in negative_offsets)
assert all((mux["frequency"] - 167_000) % 8_000_000 == 2_000_000 for mux in positive_offsets)

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

uk_auto_centre_for_frequency = load_function(
    ROOT / "app.py",
    "_uk_auto_centre_for_frequency",
    {"Any": Any, "Optional": Optional, "_coerce_int": coerce_int},
)
uk_auto_service_centres = load_function(
    ROOT / "app.py",
    "_uk_auto_service_centres",
    {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "_coerce_int": coerce_int,
        "_uk_auto_centre_for_frequency": uk_auto_centre_for_frequency,
    },
)
uk_auto_ready_centres = load_function(
    ROOT / "app.py",
    "_uk_auto_ready_centres",
    {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "_coerce_int": coerce_int,
        "_uk_auto_centre_for_frequency": uk_auto_centre_for_frequency,
    },
)
uk_auto_mux_uuids_for_centres = load_function(
    ROOT / "app.py",
    "_uk_auto_mux_uuids_for_centres",
    {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "_coerce_int": coerce_int,
        "re": re,
    },
)
assert uk_auto_centre_for_frequency(545_833_000) == 546_000_000
assert uk_auto_centre_for_frequency(546_167_000) == 546_000_000
assert uk_auto_centre_for_frequency(700_000_000) is None
assert uk_auto_service_centres([
    {"frequency": 529_833_000, "num_svc": 29},
    {"frequency": 530_000_000, "num_svc": 29},
    {"frequency": 545_833_000, "num_svc": 13},
    {"frequency": 586_000_000, "num_svc": 0},
]) == [530_000_000, 546_000_000]
assert uk_auto_ready_centres([
    {"frequency": 482_000_000, "num_svc": 26, "onid": 65_536, "tsid": 20_544},
    {"frequency": 490_000_000, "num_svc": 26, "onid": 9_018, "tsid": 4_164},
    {"frequency": 514_000_000, "num_svc": 17, "onid": 9_018, "tsid": 0},
    {"frequency": 545_833_000, "num_svc": 13, "onid": 9_018, "tsid": 16_516},
]) == [490_000_000, 546_000_000]
assert uk_auto_mux_uuids_for_centres(
    [
        {"uuid": "dvbt-nominal", "frequency": 586_000_000, "delsys": "DVB-T"},
        {"uuid": "dvbt2-nominal", "frequency": 586_000_000, "delsys": "DVB-T2"},
        {"uuid": "dvbt-offset", "frequency": 585_833_000, "delsys": "DVB-T"},
    ],
    [586_000_000],
    "DVBT",
) == ["dvbt-nominal"]

measurement_key = lambda mux: str(mux.get("frequency") or mux.get("freq") or "")
uk_auto_order_mux_targets_by_rf = load_function(
    ROOT / "app.py",
    "_uk_auto_order_mux_targets_by_rf",
    {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Tuple": Tuple,
        "_coerce_int": coerce_int,
        "_uk_auto_centre_for_frequency": uk_auto_centre_for_frequency,
        "mux_report_key": measurement_key,
        "re": re,
    },
)
rf_order_muxes = [
    {"uuid": "weak-t", "frequency": 482_000_000, "delsys": "DVB-T"},
    {"uuid": "strong-t2", "frequency": 506_000_000, "delsys": "DVB-T2"},
    {"uuid": "strong-t", "frequency": 506_000_000, "delsys": "DVB-T"},
    {"uuid": "weak-t2", "frequency": 481_833_000, "delsys": "DVB-T2"},
]
assert uk_auto_order_mux_targets_by_rf(
    rf_order_muxes,
    [mux["uuid"] for mux in rf_order_muxes],
    {
        "482000000": {"dbm_values": [-48.0], "cnr_values": [12.0]},
        "506000000": {"dbm_values": [-39.0], "cnr_values": [18.0]},
    },
) == ["strong-t", "strong-t2", "weak-t", "weak-t2"]

rf_candidate_logs = []
rf_candidate_config_calls = []


def rf_candidate_config(name, default, **_kwargs):
    rf_candidate_config_calls.append((name, default))
    return default


uk_auto_rf_candidate_centres = load_function(
    ROOT / "app.py",
    "_uk_auto_rf_candidate_centres",
    {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "_uk_auto_centre_for_frequency": uk_auto_centre_for_frequency,
        "_config_int": rf_candidate_config,
        "_tv_setup_log": rf_candidate_logs.append,
        "median": median,
        "mux_report_key": measurement_key,
    },
)
rf_candidate_muxes = [
    {"frequency": 474_000_000},
    {"frequency": 482_000_000},
    {"frequency": 490_000_000},
    {"frequency": 498_000_000},
    {"frequency": 506_000_000},
    {"frequency": 514_000_000},
    {"frequency": 522_000_000},
    {"frequency": 530_000_000},
    {"frequency": 538_000_000},
    {"frequency": 546_000_000},
    {"frequency": 554_000_000},
    {"frequency": 562_000_000},
    {"frequency": 570_000_000},
    {"frequency": 578_000_000},
    {"frequency": 586_000_000},
    {"frequency": 594_000_000},
]
rf_candidate_measurements = {
    "474000000": {"dbm_values": [-60.0]},
    "482000000": {"dbm_values": [-38.0]},
    "490000000": {"dbm_values": [-39.0], "cnr_values": [15.0]},
    "498000000": {"dbm_values": [-58.0]},
    "506000000": {"dbm_values": [-38.1]},
    "514000000": {"dbm_values": [-38.2]},
    "522000000": {"dbm_values": [-59.0]},
    "530000000": {"dbm_values": [-70.0], "cnr_values": [8.0]},
    "538000000": {"dbm_values": [-61.0]},
    "546000000": {"dbm_values": [-60.0]},
    "554000000": {"dbm_values": [-59.0]},
    "562000000": {"dbm_values": [-58.0]},
    "570000000": {"dbm_values": [-57.0]},
    "578000000": {"dbm_values": [-56.0]},
    "586000000": {"dbm_values": [-49.3]},
    "594000000": {"dbm_values": [-55.0]},
}
assert uk_auto_rf_candidate_centres(
    rf_candidate_muxes,
    rf_candidate_measurements,
) == [
    482_000_000,
    490_000_000,
    506_000_000,
    514_000_000,
    530_000_000,
    586_000_000,
]
assert rf_candidate_config_calls == [("tvh_auto_rf_candidate_margin_db", 4)]
assert "retry threshold" in rf_candidate_logs[-1]

mux_tuning_specificity = load_function(
    ROOT / "app.py",
    "_mux_tuning_specificity",
    {"Any": Any, "Dict": Dict},
)
assert mux_tuning_specificity({
    "constellation": "QAM/64",
    "transmission_mode": "8k",
    "guard_interval": "1/32",
    "fec_hi": "2/3",
}) == 4
assert mux_tuning_specificity({
    "constellation": "QAM/AUTO",
    "transmission_mode": "AUTO",
    "guard_interval": "AUTO",
    "fec_hi": "AUTO",
}) == 0

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
set_mux_tuning = load_function(
    ROOT / "tvh.py",
    "set_mux_tuning",
    {"Any": Any, "Dict": Dict, "List": List, "json": json},
)
assert set_mux_tuning(
    fake_tvh,
    ["mux-a", "mux-a", "", "mux-b"],
    {
        "constellation": "QPSK",
        "fec_hi": "3/4",
        "transmission_mode": "8k",
        "guard_interval": "1/32",
        "frequency": 586_000_000,
    },
) == 2
assert [json.loads(data["node"]) for _path, data in fake_tvh.calls] == [
    {
        "uuid": "mux-a",
        "constellation": "QPSK",
        "fec_hi": "3/4",
        "transmission_mode": "8k",
        "guard_interval": "1/32",
    },
    {
        "uuid": "mux-b",
        "constellation": "QPSK",
        "fec_hi": "3/4",
        "transmission_mode": "8k",
        "guard_interval": "1/32",
    },
]
fake_tvh.calls.clear()

scan_muxes(fake_tvh, ["mux-a", "mux-a", "", "mux-b"])
assert [json.loads(data["node"]) for _path, data in fake_tvh.calls] == [
    {"uuid": "mux-a", "scan_state": 3},
    {"uuid": "mux-b", "scan_state": 3},
]

cancel_scan_muxes = load_function(
    ROOT / "tvh.py",
    "cancel_scan_muxes",
    {"List": List, "json": json},
)
fake_tvh.calls.clear()
assert cancel_scan_muxes(fake_tvh, ["mux-a", "mux-a", "", "mux-b"]) == 2
assert [json.loads(data["node"]) for _path, data in fake_tvh.calls] == [
    {"uuid": "mux-a", "scan_state": 0},
    {"uuid": "mux-b", "scan_state": 0},
]

create_muxes = load_function(
    ROOT / "tvh.py",
    "create_muxes",
    {"Any": Any, "Dict": Dict, "List": List},
)


class FakeMuxCreator:
    def __init__(self):
        self.created = []

    def list_muxes_for_network(self, _network_uuid):
        return [{"uuid": "existing", "delsys": "DVB-T2", "frequency": 546_167_000}]

    def delete_muxes(self, _uuids):
        raise AssertionError("Incremental mux creation must not delete existing muxes")

    def create_mux(self, _network_uuid, conf):
        self.created.append(conf)
        return {}


fake_mux_creator = FakeMuxCreator()
create_result = create_muxes(
    fake_mux_creator,
    "network",
    [
        {"delsys": "DVB-T2", "frequency": 546_167_000},
        {"delsys": "DVB-T2", "frequency": 545_833_000},
        {"delsys": "DVB-T2", "frequency": 545_833_000},
    ],
)
assert create_result == {
    "deleted": 0,
    "created": 1,
    "skipped": 2,
    "errors": [],
}
assert fake_mux_creator.created == [
    {"delsys": "DVB-T2", "frequency": 545_833_000},
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

set_scan_grace = load_function(
    ROOT / "tvh.py",
    "set_dvbt_scan_grace",
    {"List": List, "Dict": Dict, "json": json},
)
restore_scan_grace = load_function(
    ROOT / "tvh.py",
    "set_dvbt_scan_grace_values",
    {"Dict": Dict, "json": json},
)
fake_grace_tvh.calls.clear()
exact_grace_results = set_scan_grace(fake_grace_tvh, 5)
assert [result["previous"] for result in exact_grace_results] == [5, 30]
assert [result["current"] for result in exact_grace_results] == [5, 5]
assert [json.loads(data["node"]) for _path, data in fake_grace_tvh.calls] == [
    {"uuid": "custom", "grace_period": 5},
]
fake_grace_tvh.calls.clear()
assert restore_scan_grace(fake_grace_tvh, {"short": 5, "custom": 30}) == 2
assert [json.loads(data["node"]) for _path, data in fake_grace_tvh.calls] == [
    {"uuid": "short", "grace_period": 5},
    {"uuid": "custom", "grace_period": 30},
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
assert "tvSetupRunning ? null : loadChannels(true)" in index_html
assert "function rfCnrLabel(rf)" in common_js
assert "function rfSignalStatsLabel(rf)" in common_js
assert "| C/N ${rfCnrLabel(rf)}" in common_js

app_source = (ROOT / "app.py").read_text(encoding="utf-8")
assert 'muxes_scanned=summary["complete"]' in app_source
assert 'muxes_total=summary["muxes"]' in app_source
assert 'services_found=network_summary["services"]' in app_source
assert 'services_found=scan_summary["services"]' in app_source
assert "No DVB-T2 multiplex locked" in app_source
assert '"cnr_db": cnr_db' in app_source
assert '"cnr_label": cnr_label' in app_source
assert "ensure_dvbt_scan_grace(scan_grace_s)" in app_source
assert "_wait_for_component_retries(" in app_source
assert "Service readiness after retry:" in app_source
assert "_run_staged_uk_auto_scan(network_uuid, scan_grace_s)" in app_source
assert "tvh.uk_auto_nominal_muxes()" in app_source
assert "_uk_auto_rf_candidate_centres(muxes, measurements)" in app_source
assert "_uk_auto_ready_centres(muxes)" in app_source
assert "_uk_auto_order_mux_targets_by_rf(" in app_source
assert "_scan_uk_auto_muxes_individually(" in app_source
assert '"tvh_auto_recovery_passes"' in app_source
assert "for recovery_pass in range(1, max_recovery_passes + 1)" in app_source
assert '"tvh_auto_recovery_mux_timeout_s"' in app_source
assert '"tvh_auto_offset_mux_timeout_s"' in app_source
assert '"tvh_bounded_profile_max_muxes"' in app_source
assert '"tvh_profile_mux_timeout_s"' in app_source
assert "Using bounded per-mux scanning for this" in app_source
assert "Retrying unresolved RF multiplexes" in app_source
assert "above-noise DVB-T2 candidate(s)" in app_source
assert "cancelled this mux and continued" in app_source
assert "_deduplicate_transport_muxes(network_uuid, muxes, measurements)" in app_source
assert "offsets=[offset_hz]" in app_source
assert '(-167_000, "negative", 52, 62)' in app_source
assert '(167_000, "positive", 62, 72)' in app_source
assert "tvh.cancel_scan_muxes(active_uuids)" in app_source
assert '"after the service acquisition retry"' in app_source
assert '"after TV Setup failed"' in app_source

configure_source = (ROOT / "packaging" / "debian" / "configure-tvheadend").read_text(encoding="utf-8")
assert 'frontend["grace_period"] = 20' in configure_source

print("UK DVB-T/T2 tuning profile checks passed.")
