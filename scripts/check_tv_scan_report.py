#!/usr/bin/env python3
"""Validate the TV scan PDF report and its deployment wiring."""

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scan_report import _mux_rows, build_scan_report, mux_report_key


def sample_mux(index: int) -> dict:
    return {
        "uuid": f"mux-{index:03d}",
        "name": f"UK UHF mux {index + 21}",
        "frequency": (474 + (index * 8)) * 1_000_000,
        "delsys": "DVBT2" if index % 3 == 0 else "DVBT",
        "scan_result": 1 if index % 11 else 2,
        "num_svc": 0 if index % 11 == 0 else 5 + (index % 7),
    }


muxes = [sample_mux(index) for index in range(48)]
measurements = {}
for index, mux in enumerate(muxes):
    if index % 9 == 0:
        continue
    measurements[mux_report_key(mux)] = {
        "samples": 3,
        "dbm_values": [-61.0 - index, -60.5 - index, -60.0 - index],
        "cnr_values": [27.0 - (index / 10), 27.5 - (index / 10), 28.0 - (index / 10)],
    }

rows = _mux_rows(muxes, measurements)
assert len(rows) == len(muxes)
assert rows[0]["dbm"] == "N/A"
assert rows[1]["dbm"].endswith(" dBm")
assert rows[1]["cnr"].endswith(" dB")
assert rows[1]["samples"] == 3

def validate_pdf(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    result = build_scan_report(
        pdf_path=output,
        logo_path=ROOT / "static" / "teletool-logo.png",
        identity={
            "hostname": "TeleTool-Test",
            "ip_address": "192.168.0.131",
            "version": "V1.8.17",
        },
        summary={
            "result": "Partially complete",
            "scanfile": "Generic Auto Default",
            "services_found": 186,
            "finished_at": 1785196800,
            "note": "One mux did not complete before the scan stopped making progress.",
        },
        muxes=muxes,
        measurements=measurements,
    )
    assert result == {"path": output, "format": "pdf"}
    assert output.read_bytes().startswith(b"%PDF-")
    assert output.stat().st_size > 20_000
    assert not output.with_suffix(".csv").exists()


preview_path = str(sys.argv[1] if len(sys.argv) > 1 else "").strip()
if preview_path:
    validate_pdf(Path(preview_path))
else:
    with tempfile.TemporaryDirectory(prefix="teletool-report-test-") as temporary:
        validate_pdf(Path(temporary) / "teletool-tv-scan-report.pdf")

app_source = (ROOT / "app.py").read_text(encoding="utf-8")
ui_source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
control_source = (ROOT / "packaging" / "debian" / "control.in").read_text(encoding="utf-8")
build_source = (ROOT / "scripts" / "build_deb.sh").read_text(encoding="utf-8")

for needle in (
    '@app.get("/api/tv/setup/report")',
    "_capture_scan_rf_measurements",
    "_clear_tv_scan_report()",
    "report_available",
):
    assert needle in app_source, needle
assert 'id="downloadTvScanReport"' in ui_source
assert "python3-reportlab" in control_source
assert "scan_report.py" in build_source

print("TV scan PDF report generation and deployment wiring are valid.")
