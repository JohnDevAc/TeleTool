#!/usr/bin/env python3
"""Reject legacy Tvheadend boolean permissions in either setup path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    ROOT / "packaging" / "debian" / "configure-tvheadend",
    ROOT / "scripts" / "pi_full_setup.sh",
)
EXPECTED_COUNTS = {
    '"streaming": ["basic", "advanced", "htsp"]': 2,
    '"profile": []': 2,
    '"dvr": ["basic", "htsp", "all", "all_rw", "failed"]': 2,
    '"dvr_config": []': 2,
}
LEGACY_VALUES = (
    '"streaming": True',
    '"profile": True',
    '"dvr": True',
    '"dvr_config": True',
)


for target in TARGETS:
    text = target.read_text(encoding="utf-8")
    for legacy in LEGACY_VALUES:
        if legacy in text:
            raise SystemExit(f"{target}: legacy permission remains: {legacy}")
    for expected, count in EXPECTED_COUNTS.items():
        actual = text.count(expected)
        if actual != count:
            raise SystemExit(
                f"{target}: expected {count} occurrences of {expected}, found {actual}"
            )

print("Tvheadend access schema is current in both setup paths.")
