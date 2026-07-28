#!/usr/bin/env python3
"""Validate Inferno PTP readiness and its packaged clock configuration."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inferno_status import inferno_clock_status_from_payload  # noqa: E402


def payload(state):
    return {"instance": {"port_ds": [{"port_state": state}]}}


ready = inferno_clock_status_from_payload(payload("Slave"))
assert ready["ready"] is True
assert ready["state"] == "Slave"

for state in ("Listening", "Master", "PreMaster", 4, 6):
    unavailable = inferno_clock_status_from_payload(payload(state))
    assert unavailable["ready"] is False
    assert "grandmaster or primary leader" in unavailable["details"]

synchronizing = inferno_clock_status_from_payload(payload("Uncalibrated"))
assert synchronizing["ready"] is False
assert "still in progress" in synchronizing["details"]

faulty = inferno_clock_status_from_payload(payload("Faulty"))
assert faulty["ready"] is False
assert "Faulty" in faulty["details"]

clock_config = (ROOT / "packaging" / "inferno" / "write-statime-config").read_text(
    encoding="utf-8"
)
for required in (
    'slave-only = true',
    '[observability]',
    'observation-path = "$observation_path"',
    "observation-permissions = 438",
):
    if required not in clock_config:
        raise SystemExit(f"Missing Inferno clock readiness configuration: {required}")

build_script = (ROOT / "scripts" / "build_deb.sh").read_text(encoding="utf-8")
if "inferno_status.py" not in build_script:
    raise SystemExit("The TeleTool package does not include inferno_status.py")

print("Inferno PTP readiness tests passed.")
