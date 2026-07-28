#!/usr/bin/env python3
"""Validate the managed UK DVB-T/T2 tuning profile without runtime imports."""

import ast
import collections
import re
from pathlib import Path
from typing import Any, Dict, List


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

assert len(muxes) == 56, f"Expected 56 UK auto muxes, found {len(muxes)}"
assert systems == {"DVB-T": 28, "DVB-T2": 28}, systems
assert min(mux["frequency"] for mux in muxes) == 474_000_000
assert max(mux["frequency"] for mux in muxes) == 690_000_000
assert len({(mux["frequency"], mux["delsys"]) for mux in muxes}) == 56

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

index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
assert "Generic Auto Legacy (DVB-T only)" in index_html
assert "teletool-uk-auto-dvbt-dvbt2" in index_html
profile_helper = index_html.index("function isTeleToolUkAuto(value)")
profile_preference = index_html.index("function preferredTvSetupRegion(options, selected)")
profile_loader = index_html.index("async function loadTvSetupRegions()")
assert profile_helper < profile_preference < profile_loader
assert "const isTeleToolUkAuto" not in index_html

print("UK DVB-T/T2 tuning profile checks passed.")
