#!/usr/bin/env python3
"""Validate lightweight system-temperature reading and API exposure."""

import ast
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    system_source = (ROOT / "system_manager.py").read_text(encoding="utf-8")
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    ast.parse(system_source, filename="system_manager.py")
    ast.parse(app_source, filename="app.py")

    system_tree = ast.parse(system_source, filename="system_manager.py")
    temperature_node = next(
        node
        for node in system_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_read_system_temperature_c"
    )
    temperature_source = ast.get_source_segment(system_source, temperature_node)
    if not temperature_source:
        raise SystemExit("Could not load the system temperature reader")
    namespace = {"Path": Path, "List": List, "Tuple": Tuple, "Optional": Optional}
    exec(compile(temperature_source, "system_manager.py", "exec"), namespace)
    read_temperature = namespace["_read_system_temperature_c"]

    with tempfile.TemporaryDirectory() as temp_dir:
        thermal_root = Path(temp_dir)
        other = thermal_root / "thermal_zone0"
        other.mkdir()
        (other / "type").write_text("gpu-thermal\n", encoding="utf-8")
        (other / "temp").write_text("61000\n", encoding="utf-8")

        cpu = thermal_root / "thermal_zone1"
        cpu.mkdir()
        (cpu / "type").write_text("cpu-thermal\n", encoding="utf-8")
        (cpu / "temp").write_text("52375\n", encoding="utf-8")

        assert read_temperature(thermal_root) == 52.4

    assert '@router.get("/api/system/temperature")' in system_source
    assert 'st["system_temperature_c"]' in app_source
    assert "subprocess" not in temperature_source

    print("System temperature API checks passed")


if __name__ == "__main__":
    main()
