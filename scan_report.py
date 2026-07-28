"""Generate the replaceable TeleTool TV scan report."""

from __future__ import annotations

import html
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SCAN_RESULT_LABELS = {
    0: "Not scanned",
    1: "OK",
    2: "Failed",
    3: "Partial",
    4: "Ignored",
}


def mux_report_key(mux: Dict[str, Any]) -> str:
    uuid = str(mux.get("uuid") or "").strip()
    if uuid:
        return uuid
    frequency = str(mux.get("frequency") or mux.get("freq") or "").strip()
    delivery = str(mux.get("delsys") or mux.get("delivery_system") or "").strip()
    return f"{delivery}:{frequency}"


def _number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _average(values: Iterable[Any]) -> Optional[float]:
    numbers = [number for value in values if (number := _number(value)) is not None]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def _format_db(value: Optional[float], unit: str) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1f} {unit}"


def _frequency_mhz(mux: Dict[str, Any]) -> Optional[float]:
    value = _number(mux.get("frequency") or mux.get("freq"))
    if value is None:
        return None
    return value / 1_000_000.0 if value >= 1_000_000 else value


def _frequency_label(mux: Dict[str, Any]) -> str:
    frequency = _frequency_mhz(mux)
    return f"{frequency:.3f} MHz" if frequency is not None else "N/A"


def _scan_result_label(mux: Dict[str, Any]) -> str:
    raw = mux.get("scan_result")
    if isinstance(raw, str) and raw.strip() and not raw.strip().isdigit():
        return raw.strip()
    result = _integer(raw, -1)
    if result in SCAN_RESULT_LABELS:
        return SCAN_RESULT_LABELS[result]
    status = str(mux.get("scan_status") or mux.get("status") or "").strip()
    return status or "Unknown"


def _mux_name(mux: Dict[str, Any]) -> str:
    return str(mux.get("name") or mux.get("muxname") or mux.get("uuid") or "Mux").strip()


def _delivery_label(mux: Dict[str, Any]) -> str:
    raw = str(mux.get("delsys") or mux.get("delivery_system") or "").strip()
    normalized = "".join(character for character in raw.upper() if character.isalnum())
    if normalized == "DVBT2":
        return "DVB-T2"
    if normalized == "DVBT":
        return "DVB-T"
    return raw or "N/A"


def _mux_rows(
    muxes: List[Dict[str, Any]],
    measurements: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for mux in sorted(
        muxes,
        key=lambda item: (
            _frequency_mhz(item) if _frequency_mhz(item) is not None else float("inf"),
            str(item.get("delsys") or ""),
        ),
    ):
        measurement = measurements.get(mux_report_key(mux), {})
        dbm = _average(measurement.get("dbm_values") or [])
        cnr = _average(measurement.get("cnr_values") or [])
        rows.append({
            "name": _mux_name(mux),
            "frequency": _frequency_label(mux),
            "delivery": _delivery_label(mux),
            "result": _scan_result_label(mux),
            "services": _integer(mux.get("num_svc")),
            "dbm": _format_db(dbm, "dBm"),
            "cnr": _format_db(cnr, "dB"),
            "samples": _integer(measurement.get("samples")),
        })
    return rows


def _generated_at_label(timestamp: Any) -> str:
    value = _number(timestamp)
    moment = datetime.fromtimestamp(value).astimezone() if value is not None else datetime.now().astimezone()
    return moment.strftime("%Y-%m-%d %H:%M:%S %Z")


def _atomic_target(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(handle)
    return Path(temporary)


def _write_pdf_report(
    output_path: Path,
    *,
    logo_path: Path,
    identity: Dict[str, Any],
    summary: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    temporary = _atomic_target(output_path)
    page_size = landscape(A4)
    blue = colors.HexColor("#008CE3")
    navy = colors.HexColor("#101721")
    yellow = colors.HexColor("#FFD500")
    pale_blue = colors.HexColor("#EAF6FD")
    pale_red = colors.HexColor("#FDEEEE")
    grid = colors.HexColor("#CCD5DF")
    text = colors.HexColor("#17202A")
    muted = colors.HexColor("#52606D")
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TeleToolTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=23,
        textColor=navy,
        alignment=TA_LEFT,
        spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "TeleToolSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=muted,
    )
    section_style = ParagraphStyle(
        "TeleToolSection",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=12,
        textColor=navy,
        spaceBefore=4,
        spaceAfter=5,
    )
    cell_style = ParagraphStyle(
        "TeleToolCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=6.7,
        leading=8,
        textColor=text,
    )
    note_style = ParagraphStyle(
        "TeleToolNote",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=text,
    )

    def safe(value: Any) -> str:
        return html.escape(str(value if value not in (None, "") else "N/A"))

    def draw_page(canvas, document) -> None:
        canvas.saveState()
        width, height = page_size
        canvas.setFillColor(blue)
        canvas.rect(0, height - 4, width, 4, fill=1, stroke=0)
        canvas.setFillColor(yellow)
        canvas.rect(0, height - 6, width, 2, fill=1, stroke=0)
        canvas.setFillColor(muted)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(12 * mm, 8 * mm, "TeleTool TV Scan Report")
        canvas.drawRightString(width - 12 * mm, 8 * mm, f"Page {document.page}")
        canvas.restoreState()

    story = []
    logo = None
    if logo_path.is_file():
        logo = Image(str(logo_path), width=32 * mm, height=24 * mm, kind="proportional")
    heading = [
        Paragraph("TV Scan Report", title_style),
        Paragraph(
            f"{safe(identity.get('hostname') or 'TeleTool')} &nbsp;&nbsp; "
            f"{safe(identity.get('ip_address'))} &nbsp;&nbsp; "
            f"{safe(identity.get('version'))}",
            subtitle_style,
        ),
    ]
    heading_table = Table(
        [[logo or "", heading]],
        colWidths=[38 * mm, 225 * mm],
        hAlign="LEFT",
    )
    heading_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.extend([heading_table, Spacer(1, 5 * mm)])

    summary_data = [
        ["Generated", _generated_at_label(summary.get("finished_at")), "Result", summary.get("result") or "Unknown"],
        ["Scan profile", summary.get("scanfile") or "Existing mux list", "Muxes", len(rows)],
        ["Services found", _integer(summary.get("services_found")), "RF sample interval", "3 seconds"],
    ]
    summary_table = Table(summary_data, colWidths=[28 * mm, 105 * mm, 32 * mm, 98 * mm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), pale_blue),
        ("TEXTCOLOR", (0, 0), (-1, -1), text),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.35, grid),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([summary_table, Spacer(1, 3 * mm)])

    if summary.get("note"):
        story.append(Paragraph(f"<b>Note:</b> {safe(summary.get('note'))}", note_style))
        story.append(Spacer(1, 1.5 * mm))
    if summary.get("error"):
        story.append(Paragraph(f"<b>Error:</b> {safe(summary.get('error'))}", note_style))
        story.append(Spacer(1, 1.5 * mm))

    story.extend([Paragraph("Mux Results", section_style)])
    table_data = [[
        "Mux",
        "Frequency",
        "System",
        "Result",
        "Services",
        "Signal avg",
        "C/N avg",
        "Samples",
    ]]
    for row in rows:
        table_data.append([
            Paragraph(safe(row["name"]), cell_style),
            row["frequency"],
            row["delivery"],
            row["result"],
            row["services"],
            row["dbm"],
            row["cnr"],
            row["samples"],
        ])

    mux_table = Table(
        table_data,
        repeatRows=1,
        colWidths=[69 * mm, 28 * mm, 20 * mm, 28 * mm, 18 * mm, 28 * mm, 26 * mm, 19 * mm],
        hAlign="LEFT",
    )
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 6.7),
        ("GRID", (0, 0), (-1, -1), 0.3, grid),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for index, row in enumerate(rows, start=1):
        if row["result"].lower() == "failed":
            table_style.append(("BACKGROUND", (0, index), (-1, index), pale_red))
        elif index % 2 == 0:
            table_style.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#F7F9FB")))
    mux_table.setStyle(TableStyle(table_style))
    story.append(mux_table)

    document = SimpleDocTemplate(
        str(temporary),
        pagesize=page_size,
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=14 * mm,
        title="TeleTool TV Scan Report",
        author="TeleTool",
        subject="DVB-T/T2 mux scan results",
    )
    try:
        document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def build_scan_report(
    *,
    pdf_path: Path,
    logo_path: Path,
    identity: Dict[str, Any],
    summary: Dict[str, Any],
    muxes: List[Dict[str, Any]],
    measurements: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    rows = _mux_rows(muxes, measurements)
    _write_pdf_report(
        pdf_path,
        logo_path=logo_path,
        identity=identity,
        summary=summary,
        rows=rows,
    )
    return {"path": pdf_path, "format": "pdf"}
