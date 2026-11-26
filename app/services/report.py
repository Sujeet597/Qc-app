"""QC report - BRD section 8, as Excel and PDF.

Contents: inspection ID / date / vendor, PO / ASN / article / color / size, approved PPS / sealer
references, checklist point with expected standard, AI result and confidence, value read or measured,
captured evidence (every attempt), failure reason and corrective action, QC reviewer, final approval
status, reinspection history and closure date.
"""
import io
from dataclasses import dataclass, field

from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image

from app.checklist import BY_NO
from app.models import Inspection, InspectionStatus
from app.services import storage
from app.services.decision import summarize

_FILL = {"pass": "C6EFCE", "fail": "FFC7CE", "manual_review": "FFEB9C", "pending": "EDEDED"}
_CLOSED = {InspectionStatus.PASSED, InspectionStatus.FAILED}


@dataclass
class Reference:
    kind: str
    color_name: str | None
    size: str | None
    note: str
    file_path: str


@dataclass
class ReportExtras:
    """Data the report needs from outside the inspection row: user names and the approved references."""
    names: dict[int, str] = field(default_factory=dict)
    references: list[Reference] = field(default_factory=list)


def _when(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def _closed(insp) -> str:
    """Closure date: when the final decision was Passed or Failed (Reinspection keeps the case open)."""
    return _when(insp.decided_at) if insp.status in _CLOSED else ""


def _name(extras: ReportExtras, user_id) -> str:
    return extras.names.get(user_id, f"user {user_id}") if user_id else ""


def _header(insp, extras: ReportExtras) -> list[tuple[str, object]]:
    po = insp.po
    s = summarize(insp)
    return [
        ("Inspection ID", insp.id), ("Created", _when(insp.created_at)),
        ("Vendor", po.vendor_code), ("PO", po.po_number), ("ASN", po.asn_number or ""),
        ("Article", po.article_number), ("Color", insp.color_name), ("Size", insp.size),
        ("Final approval status", insp.status), ("Summary", s["state"]), ("Progress", s["progress"]),
        ("Decided by", _name(extras, getattr(insp, "decided_by_id", None))),
        ("Decision note", insp.decision_note),
        ("Exception recorded", "Yes" if getattr(insp, "exception_recorded", False) else ""),
        ("Decided", _when(insp.decided_at)), ("Closure date", _closed(insp)),
    ]


def _attempt_text(chk, e) -> str:
    text = f"{BY_NO[chk.check_no].title} - {e.captured_at:%Y-%m-%d %H:%M:%S}"
    outcome = (e.extracted or {}).get("outcome")
    if outcome:  # older evidence rows have no per-attempt result
        text += f"\n{outcome['status']}: {outcome['reason']}"
    if (e.extracted or {}).get("replaced_review"):
        r = e.extracted["replaced_review"]
        text += f"\n(replaced QC review: {r['status']} - {r['comment']})"
    return text


def _put(ws, row: int, col: int, value):
    """Write a cell. Text is always stored as text: openpyxl would otherwise turn vendor / QC text that
    starts with '=' (e.g. =HYPERLINK(...)) into a live formula in the QC report."""
    cell = ws.cell(row, col, value)
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def _xl_image(ws, path: str, anchor: str, row: int) -> None:
    small = _jpeg(path, 480)   # embed a small copy, not the full-resolution photo
    try:
        if small is None:
            raise ValueError("image unavailable")
        img = XLImage(small)
        img.width, img.height = 240, int(240 * img.height / max(img.width, 1))
        ws.add_image(img, anchor)
        ws.row_dimensions[row].height = img.height * 0.75
    except Exception:
        ws.cell(row, 3, "image unavailable")


def build_excel(insp: Inspection, history: list[Inspection], extras: ReportExtras | None = None) -> bytes:
    extras = extras or ReportExtras()
    wb = Workbook()
    ws = wb.active
    ws.title = "QC Report"
    header = _header(insp, extras)
    for r, (k, v) in enumerate(header, 1):
        _put(ws, r, 1, k).font = Font(bold=True)
        _put(ws, r, 2, v)

    start = len(header) + 2
    cols = ["No", "Checklist point", "Critical", "Expected", "Observed", "Result", "AI result",
            "Confidence", "Reason", "Reviewer", "Reviewer comment", "Corrective action", "Evidence"]
    for c, name in enumerate(cols, 1):
        cell = ws.cell(start, c, name)
        cell.font = Font(bold=True)
    widths = [5, 34, 9, 30, 30, 14, 14, 11, 40, 18, 30, 30, 22]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w

    for i, chk in enumerate(insp.checks, start + 1):
        d = BY_NO[chk.check_no]
        row = [chk.check_no, d.title, "Yes" if d.critical else "", chk.expected_value, chk.observed_value,
               chk.status, chk.ai_status or "", chk.confidence, chk.reason,
               _name(extras, getattr(chk, "reviewer_id", None)), chk.reviewer_comment,
               chk.corrective_action, f"{len(chk.evidence)} image(s)"]
        for c, v in enumerate(row, 1):
            _put(ws, i, c, v).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(i, 6).fill = PatternFill("solid", fgColor=_FILL.get(chk.status, "FFFFFF"))

    # Evidence sheet: every capture attempt with its own result
    ev = wb.create_sheet("Evidence")
    ev.column_dimensions["B"].width = 40
    row = 1
    for chk in insp.checks:
        for e in chk.evidence:
            ev.cell(row, 1, chk.check_no)
            _put(ev, row, 2, _attempt_text(chk, e)).alignment = Alignment(wrap_text=True, vertical="top")
            _xl_image(ev, e.file_path, f"C{row}", row)
            row += 1

    # Approved PPS / sealer / label references the checks were compared against
    refs = wb.create_sheet("Approved references")
    refs.column_dimensions["B"].width = 40
    for row, ref in enumerate(extras.references, 1):
        _put(refs, row, 1, ref.kind)
        _put(refs, row, 2, f"color: {ref.color_name or 'all'}, size: {ref.size or 'all'}\n{ref.note}").alignment = \
            Alignment(wrap_text=True, vertical="top")
        _xl_image(refs, ref.file_path, f"C{row}", row)

    hist = wb.create_sheet("Reinspection history")
    hist.append(["Inspection ID", "Created", "Status", "Decided", "Closure date", "Note"])
    for r, h in enumerate(history, 2):
        for c, v in enumerate([h.id, _when(h.created_at), h.status, _when(h.decided_at), _closed(h), h.decision_note], 1):
            _put(hist, r, c, v)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------- PDF ----------------

def _latin1(value) -> str:
    """The built-in PDF fonts are Latin-1 only: replace anything else instead of failing."""
    text = "" if value is None else str(value)
    text = text.replace("–", "-").replace("—", "-").replace("≤", "<=").replace("≥", ">=")
    return text.encode("latin-1", "replace").decode("latin-1")


def _jpeg(path: str, max_side: int = 800) -> io.BytesIO | None:
    try:
        with Image.open(io.BytesIO(storage.read_file(path))) as src:
            src.draft("RGB", (max_side, max_side))
            src.thumbnail((max_side, max_side))
            img = src.convert("RGB")
    except Exception:
        return None
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    buf.seek(0)
    return buf


class _PDF(FPDF):
    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", size=8)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def _section(pdf: FPDF, title: str) -> None:
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, _latin1(title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=8)


def _images(pdf: FPDF, items: list[tuple[str, str]], width: float = 60, max_height: float = 70) -> None:
    """Captioned images, three per row."""
    gap, per_row = 5, 3
    x0 = pdf.l_margin
    for i in range(0, len(items), per_row):
        row = [(cap, _jpeg(path)) for cap, path in items[i:i + per_row]]
        dims: list[tuple[float, float]] = []
        cap_heights: list[float] = []
        for cap, img in row:
            lines = pdf.multi_cell(width, 3.5, _latin1(cap), align="L", dry_run=True, output="LINES")
            cap_heights.append(max(1, len(lines)) * 3.5)
            if img:
                with Image.open(img) as im:
                    w = width
                    h = w * im.height / max(im.width, 1)
                    if h > max_height:
                        w = max_height * im.width / max(im.height, 1)
                        h = max_height
                    dims.append((w, h))
                img.seek(0)
            else:
                dims.append((width, 5.0))
        cap_h = max(cap_heights or [3.5])
        img_h = max((h for _, h in dims), default=5.0)
        total_h = cap_h + 2 + img_h + 4
        if pdf.get_y() + total_h > pdf.page_break_trigger:
            pdf.add_page()
        y = pdf.get_y()
        for j, ((cap, img), (w, h)) in enumerate(zip(row, dims)):
            x = x0 + j * (width + gap)
            pdf.set_xy(x, y)
            pdf.multi_cell(width, 3.5, _latin1(cap), align="L", new_x="LEFT", new_y="NEXT", max_line_height=3.5)
            img_y = y + cap_h + 1
            if img:
                pdf.image(img, x=x, y=img_y, w=w, h=h)
            else:
                pdf.set_xy(x, img_y)
                pdf.cell(width, 5, "image unavailable")
        pdf.set_xy(x0, y + total_h)


def build_pdf(insp: Inspection, history: list[Inspection], extras: ReportExtras | None = None) -> bytes:
    extras = extras or ReportExtras()
    pdf = _PDF(orientation="L", format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 10, _latin1(f"Garment QC Report - Inspection {insp.id}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", size=9)
    with pdf.table(col_widths=(45, 95, 45, 95), first_row_as_headings=False, text_align="LEFT") as table:
        pairs = _header(insp, extras)
        for i in range(0, len(pairs), 2):
            r = table.row()
            for k, v in pairs[i:i + 2]:
                r.cell(_latin1(k))
                r.cell(_latin1(v))

    _section(pdf, "Checklist results")
    with pdf.table(col_widths=(8, 38, 12, 34, 34, 18, 18, 12, 46, 22, 33), text_align="LEFT",
                   line_height=3.8) as table:
        r = table.row()
        for name in ("No", "Checklist point", "Critical", "Expected", "Observed", "Result", "AI result",
                     "Conf.", "Reason", "Reviewer", "Comment / corrective action"):
            r.cell(name)
        for chk in insp.checks:
            d = BY_NO[chk.check_no]
            conf = "" if chk.confidence is None else f"{chk.confidence:.2f}"
            comment = " / ".join(x for x in (chk.reviewer_comment, chk.corrective_action) if x)
            r = table.row()
            for v in (chk.check_no, d.title, "Yes" if d.critical else "", chk.expected_value, chk.observed_value,
                      chk.status, chk.ai_status or "", conf, chk.reason,
                      _name(extras, getattr(chk, "reviewer_id", None)), comment):
                r.cell(_latin1(v))

    if extras.references:
        _section(pdf, "Approved references")
        _images(pdf, [(f"{ref.kind} - color: {ref.color_name or 'all'}, size: {ref.size or 'all'}", ref.file_path)
                      for ref in extras.references])

    evidence = [(_attempt_text(chk, e), e.file_path) for chk in insp.checks for e in chk.evidence]
    if evidence:
        _section(pdf, "Captured evidence (every attempt)")
        _images(pdf, [(f"Check {cap}", path) for cap, path in evidence])

    _section(pdf, "Reinspection history")
    with pdf.table(col_widths=(25, 35, 40, 35, 35, 100), text_align="LEFT") as table:
        r = table.row()
        for name in ("Inspection ID", "Created", "Status", "Decided", "Closure date", "Note"):
            r.cell(name)
        for h in history:
            r = table.row()
            for v in (h.id, _when(h.created_at), h.status, _when(h.decided_at), _closed(h), h.decision_note):
                r.cell(_latin1(v))

    return bytes(pdf.output())
