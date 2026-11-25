"""Excel QC report - BRD section 8."""
import io

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill

from app.checklist import BY_NO
from app.models import Inspection
from app.services import storage
from app.services.decision import summarize

_FILL = {"pass": "C6EFCE", "fail": "FFC7CE", "manual_review": "FFEB9C", "pending": "EDEDED"}


def _put(ws, row: int, col: int, value):
    """Write a cell. Text is always stored as text: openpyxl would otherwise turn vendor / QC text that
    starts with '=' (e.g. =HYPERLINK(...)) into a live formula in the QC report."""
    cell = ws.cell(row, col, value)
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def build_excel(insp: Inspection, history: list[Inspection]) -> bytes:
    po = insp.po
    wb = Workbook()
    ws = wb.active
    ws.title = "QC Report"
    s = summarize(insp)
    header = [
        ("Inspection ID", insp.id), ("Created", insp.created_at.strftime("%Y-%m-%d %H:%M")),
        ("Vendor", po.vendor_code), ("PO", po.po_number), ("ASN", po.asn_number or ""),
        ("Article", po.article_number), ("Color", insp.color_name), ("Size", insp.size),
        ("Status", insp.status), ("Summary", s["state"]), ("Progress", s["progress"]),
        ("Decision note", insp.decision_note),
        ("Decided", insp.decided_at.strftime("%Y-%m-%d %H:%M") if insp.decided_at else ""),
    ]
    for r, (k, v) in enumerate(header, 1):
        _put(ws, r, 1, k).font = Font(bold=True)
        _put(ws, r, 2, v)

    start = len(header) + 2
    cols = ["No", "Checklist point", "Critical", "Expected", "Observed", "Result", "AI result",
            "Confidence", "Reason", "Reviewer comment", "Corrective action", "Evidence"]
    for c, name in enumerate(cols, 1):
        cell = ws.cell(start, c, name)
        cell.font = Font(bold=True)
    widths = [5, 34, 9, 30, 30, 14, 14, 11, 40, 30, 30, 22]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w

    for i, chk in enumerate(insp.checks, start + 1):
        d = BY_NO[chk.check_no]
        row = [chk.check_no, d.title, "Yes" if d.critical else "", chk.expected_value, chk.observed_value,
               chk.status, chk.ai_status or "", chk.confidence, chk.reason, chk.reviewer_comment,
               chk.corrective_action, f"{len(chk.evidence)} image(s)"]
        for c, v in enumerate(row, 1):
            _put(ws, i, c, v).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(i, 6).fill = PatternFill("solid", fgColor=_FILL.get(chk.status, "FFFFFF"))

    # Evidence sheet with thumbnails
    ev = wb.create_sheet("Evidence")
    ev.column_dimensions["B"].width = 40
    row = 1
    for chk in insp.checks:
        for e in chk.evidence:
            ev.cell(row, 1, chk.check_no)
            _put(ev, row, 2, f"{BY_NO[chk.check_no].title} - {e.captured_at:%Y-%m-%d %H:%M:%S}")
            try:
                img = XLImage(io.BytesIO(storage.read_file(e.file_path)))
                img.width, img.height = 240, int(240 * img.height / max(img.width, 1))
                ev.add_image(img, f"C{row}")
                ev.row_dimensions[row].height = img.height * 0.75
            except Exception:
                ev.cell(row, 3, "image unavailable")
            row += 1

    hist = wb.create_sheet("Reinspection history")
    hist.append(["Inspection ID", "Created", "Status", "Decided", "Note"])
    for r, h in enumerate(history, 2):
        for c, v in enumerate([h.id, f"{h.created_at:%Y-%m-%d %H:%M}", h.status,
                               f"{h.decided_at:%Y-%m-%d %H:%M}" if h.decided_at else "", h.decision_note], 1):
            _put(hist, r, c, v)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
