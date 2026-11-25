"""Runs one checklist point against one captured frame. BRD section 3 + section 4 special checks."""
import math
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.checklist import BY_NO, CheckDef, Method, input_fields
from app.core.config import settings
from app.models import CheckStatus, Inspection, NominatedVendor, ReferenceAsset
from app.services import barcode, imaging, storage
from app.services.ai_vision import AIResult, VisionTask, get_provider
from app.services.decision import from_ai

INNER_PACK_FIELD = "inner_pack_pcs"   # pieces counted in the opened inner pack (check 15)


@dataclass
class CheckInputs:
    codes: list[str] = field(default_factory=list)       # barcode values read by QC (never from the vendor)
    measured: dict = field(default_factory=dict)         # {"weight_g": 212} / {"gsm": 180} / {"inner_pack_pcs": 5}
    supplier_code: str | None = None                     # for nominated-vendor checks
    batch_no: str | None = None                          # supplier batch for checks 18-20


@dataclass
class Outcome:
    status: CheckStatus
    confidence: float | None
    expected: str
    observed: str
    reason: str
    extracted: dict = field(default_factory=dict)
    ai_decided: bool = False      # Pass / Fail came from the AI's judgement (see AI_AUTO_DECIDE)


def _norm(s: str) -> str:
    return "".join((s or "").split()).casefold()


def _tokens(s: str) -> list[str]:
    return re.findall(r"[0-9a-z]+", (s or "").casefold())


def _contains(expected: str, observed: str) -> bool:
    """True if `expected` appears in `observed` as whole words, ignoring case, spaces and punctuation.

    Unlike a substring test, size "S" does not match "SIZE L", "L" does not match "XL" and
    PO "4500123" does not match "45001234". Words may be split differently: "Navy Blue" matches "NAVYBLUE".
    """
    target = "".join(_tokens(expected))
    words = _tokens(observed)
    if not target:
        return False
    for i in range(len(words)):
        joined = ""
        for word in words[i:]:
            joined += word
            if joined == target:
                return True
            if len(joined) >= len(target):
                break
    return False


def _applies(ref: ReferenceAsset, insp: Inspection) -> bool:
    """A reference with no color / size applies to all; one for another color or size never applies."""
    if _norm(ref.color_name) and _norm(ref.color_name) != _norm(insp.color_name):
        return False
    return not (_norm(ref.size) and _norm(ref.size) != _norm(insp.size))


def applicable_references(db: Session, insp: Inspection, kind: str | None = None) -> list[ReferenceAsset]:
    """Approved references for this inspection's PO, color and size, newest first."""
    q = select(ReferenceAsset).where(ReferenceAsset.po_id == insp.po_id)
    if kind:
        q = q.where(ReferenceAsset.kind == kind)
    rows = db.scalars(q.order_by(ReferenceAsset.uploaded_at.desc(), ReferenceAsset.id.desc())).all()
    return [r for r in rows if _applies(r, insp)]


def _reference(db: Session, insp: Inspection, kind: str) -> bytes | None:
    """The approved reference for this PO, color and size (BRD 4C).

    A reference uploaded for another color or size is never used: a navy garment must not be judged
    against the red PPS. One that names this inspection's color and size is preferred, then the newest.
    """
    rows = applicable_references(db, insp, kind)
    best = max(rows, key=lambda r: 2 * bool(_norm(r.color_name)) + bool(_norm(r.size)), default=None)
    return storage.read_file(best.file_path) if best else None


def _manual(expected: str, reason: str, **extra) -> Outcome:
    return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, "", reason, extra)


def _ai_status(r: AIResult) -> CheckStatus:
    return from_ai(r.verdict, r.confidence)


def run_check(db: Session, insp: Inspection, check: CheckDef, frame: bytes, inp: CheckInputs) -> Outcome:
    out = _run_check(db, insp, check, frame, inp)
    # BRD section 10: automatic decisions only after validation against real inspections. Until the
    # pilot sets AI_AUTO_DECIDE=true, the AI's Pass / Fail is shown to QC as a suggestion.
    if out.ai_decided and out.status in (CheckStatus.PASS, CheckStatus.FAIL) and not settings.ai_auto_decide:
        out.reason = f"AI suggests {out.status.upper()} - QC to confirm. {out.reason}"
        out.status = CheckStatus.MANUAL_REVIEW
    return out


def _run_check(db: Session, insp: Inspection, check: CheckDef, frame: bytes, inp: CheckInputs) -> Outcome:
    po = insp.po
    quality = imaging.assess_quality(frame)
    q = {"sharpness": round(quality.sharpness, 1), "brightness": round(quality.brightness, 1)}
    ai_based = check.method in {Method.COLOR_COMPARE, Method.OCR_MATCH, Method.VISUAL_COMPARE,
                                Method.DEFECT_SCAN, Method.DOCUMENT_RECONCILE}
    if ai_based and not quality.ok:
        return _manual("", "Reshow item: " + " ".join(quality.issues), quality=q)

    ai = get_provider()

    match check.method:
        case Method.COLOR_COMPARE:
            lighting = imaging.lighting_issues(frame)   # BRD 4A: unreliable lighting -> QC review
            if lighting:
                return _manual("", "Reshow item: " + " ".join(lighting), quality=q)
            ref = _reference(db, insp, check.reference_kind or "pps")
            if ref is None:
                return _manual("PPS reference", "No approved PPS reference uploaded for this PO/color.", quality=q)
            de = imaging.delta_e(frame, ref)
            limit = settings.color_delta_e_max
            obs = f"Delta-E {de:.2f} (limit {limit})"
            if de <= limit:
                return Outcome(CheckStatus.PASS, 0.9, f"Delta-E <= {limit}", obs, "Color within tolerance.", {"delta_e": de, **q})
            if de > 2 * limit:
                return Outcome(CheckStatus.FAIL, 0.9, f"Delta-E <= {limit}", obs, "Color clearly differs from PPS.", {"delta_e": de, **q})
            return Outcome(CheckStatus.MANUAL_REVIEW, 0.5, f"Delta-E <= {limit}", obs,
                           "Borderline color difference - QC to confirm under standard light.", {"delta_e": de, **q})

        case Method.OCR_MATCH:
            return _ocr_match(db, insp, check, frame, q)

        case Method.BARCODE_MATCH:
            return _barcode_taffeta(insp, check, frame, inp, q)

        case Method.VISUAL_COMPARE | Method.DEFECT_SCAN:
            ref = None
            if check.method == Method.VISUAL_COMPARE:
                # No fallback to the sealer: comparing a main label or inner pack with the garment
                # sealer photo would judge it against the wrong image.
                ref = _reference(db, insp, check.reference_kind or "sealer")
                if ref is None:
                    missing = _manual("Approved sealer", f"No '{check.reference_kind}' reference uploaded.", quality=q)
                    return _inner_pack(insp, inp, missing) if check.no == 15 else missing
                task = f"Compare the live frame with the approved reference for: {check.title}. Check design, placement, color, size and visible defects."
                if check.no == 12:
                    task += " Judge only what is visible; composition and hand-feel cannot be judged on video."
                if check.no == 15:
                    task += " Check the packing method, the contents and the labels."
            else:
                task = "Inspect for creases, shine marks, burns, stains and other visible ironing / finishing defects."
            r = ai.analyze(VisionTask(check.title, task, frame, ref))
            obs = r.observed_text or "; ".join(r.defects)
            out = Outcome(_ai_status(r), r.confidence, "As per sealer" if ref else "No visible defects",
                          obs, r.reason, {"ai": r.__dict__, **q}, ai_decided=True)
            if check.no == 12 and out.status == CheckStatus.PASS:
                out.reason += " Composition / hand-feel not verifiable on video - QC to confirm if required."
            if check.no == 15:
                return _inner_pack(insp, inp, out)
            return out

        case Method.DOCUMENT_RECONCILE:
            return _packing_list(insp, check, frame, q)

        case Method.MEASUREMENT:
            return _measurement(check, insp, inp, q)

        case Method.VENDOR_MASTER:
            return _nominated_vendor(db, insp, check, frame, inp, q)

    raise ValueError(f"Unhandled method {check.method}")


def _ocr_match(db: Session, insp: Inspection, check: CheckDef, frame: bytes, q: dict) -> Outcome:
    po = insp.po
    ai = get_provider()
    expected = insp.size if check.po_field == "size" else str(getattr(po, check.po_field or "", ""))
    label = {"size": "size", "color_name": "color name", "mc": "master carton (MC) details"}.get(check.po_field, check.po_field)
    r = ai.analyze(VisionTask(check.title, f"Read the {label} exactly as printed. Put it in observed_text.", frame))
    extracted = {"ai": r.__dict__, **q}
    if r.verdict == "unsure" or not r.observed_text:
        return Outcome(CheckStatus.MANUAL_REVIEW, r.confidence, expected, r.observed_text, r.reason, extracted)
    if not _contains(expected, r.observed_text):
        return Outcome(from_ai("fail", r.confidence), r.confidence, expected, r.observed_text,
                       f"Label reads '{r.observed_text}', PO says '{expected}'.", extracted, ai_decided=True)
    if not check.reference_kind:
        return Outcome(from_ai("pass", r.confidence), r.confidence, expected, r.observed_text, "Matches PO.",
                       extracted, ai_decided=True)

    # Size label (check 5): the text must match the PO *and* the label must match the approved label standard.
    ref = _reference(db, insp, check.reference_kind)
    if ref is None:
        return Outcome(CheckStatus.MANUAL_REVIEW, r.confidence, expected, r.observed_text,
                       f"Text matches PO, but no approved '{check.reference_kind}' reference uploaded - "
                       "QC to check the label standard.", extracted)
    std = ai.analyze(VisionTask(check.title, "Compare this label with the approved label standard: design, print, "
                                             "colors, content and placement. List every difference.", frame, ref))
    extracted["ai_standard"] = std.__dict__
    confidence = min(r.confidence, std.confidence)
    if std.verdict == "unsure":
        return Outcome(CheckStatus.MANUAL_REVIEW, confidence, expected, r.observed_text,
                       f"Text matches PO; label standard not confirmed: {std.reason}", extracted)
    reason = "Matches PO and approved label standard." if std.verdict == "pass" else \
        f"Text matches PO but differs from approved label: {std.reason}"
    return Outcome(from_ai(std.verdict, confidence), confidence, f"{expected} (approved label standard)",
                   r.observed_text, reason, extracted, ai_decided=True)


def _barcode_taffeta(insp: Inspection, check: CheckDef, frame: bytes, inp: CheckInputs, q: dict) -> Outcome:
    """BRD 4B: Match / Mismatch / Unable to Read. Missing, unreadable, duplicate or mismatched identifiers are flagged."""
    po = insp.po
    codes = list(dict.fromkeys(c.strip() for c in inp.codes + barcode.decode(frame) if c and c.strip()))
    expected = f"barcode {po.barcode_number}, taffeta {po.taffeta_number}, article {po.article_number}"
    observed = ", ".join(codes)
    extracted = {"codes": codes, **q}
    known = {_norm(po.barcode_number), _norm(po.taffeta_number)}
    found_bar = po.barcode_number in codes
    found_taf = po.taffeta_number in codes
    others = [c for c in codes if _norm(c) not in known]

    if not codes:
        return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, "", "Unable to read - show labels closer.", extracted)
    if not found_bar:
        if not others:  # only the taffeta code was seen
            return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, observed,
                           "Unable to read the barcode label - show it closer.", extracted)
        return Outcome(CheckStatus.FAIL, 1.0, expected, observed, "Barcode does not match PO.", extracted)

    # Read the printed identifiers of both labels: taffeta number (often text, not a barcode) and article / style.
    r = get_provider().analyze(VisionTask(
        check.title, "Read every identifier printed on the barcode label and on the taffeta label exactly as printed "
                     "(article number, style number, taffeta number, color, size). Put them all in observed_text.", frame))
    extracted["ai"] = r.__dict__
    text = r.observed_text if r.verdict != "unsure" else ""
    taffeta_by_ai = not found_taf and _contains(po.taffeta_number, text)
    found_taf = found_taf or taffeta_by_ai
    article_seen = _contains(po.article_number, text) or _contains(po.style_number, text)

    if others:
        return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, observed,
                       f"Extra / duplicate code(s) on the labels: {', '.join(others)} - QC to check the labels match each other.",
                       extracted)
    if not found_taf:
        return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, observed, "Barcode OK, taffeta number not confirmed.", extracted)
    if text and not article_seen:
        return Outcome(CheckStatus.MANUAL_REVIEW, r.confidence, expected, f"{observed}; labels read: {text}",
                       f"Article {po.article_number} / style {po.style_number} not found on the labels - QC to verify.",
                       extracted)
    article_note = (f" Article / style {po.article_number} found on labels." if article_seen
                    else " Article / style text not read - identified by the barcode.")
    if taffeta_by_ai:
        return Outcome(from_ai("pass", r.confidence), r.confidence, expected, f"{observed}; labels read: {text}",
                       "Barcode matches PO; taffeta number read from the label." + article_note, extracted, ai_decided=True)
    return Outcome(CheckStatus.PASS, 1.0, expected, observed, "Barcode and taffeta match PO." + article_note, extracted)


def _packing_list(insp: Inspection, check: CheckDef, frame: bytes, q: dict) -> Outcome:
    """BRD check 14: reconcile article, color, size, quantity and PO details."""
    po = insp.po
    need = {"PO": po.po_number, "article": po.article_number, "color": insp.color_name, "size": insp.size}
    if po.asn_number:
        need["ASN"] = po.asn_number
    if po.order_qty:
        need["quantity"] = str(po.order_qty)
    r = get_provider().analyze(VisionTask(
        check.title, "Read this packing list. Copy the PO number, ASN number, article, color, sizes and quantities "
                     "into observed_text.", frame))
    expected = ", ".join(f"{k} {v}" for k, v in need.items())
    extracted = {"ai": r.__dict__, **q}
    if r.verdict == "unsure":
        return Outcome(CheckStatus.MANUAL_REVIEW, r.confidence, expected, r.observed_text, r.reason, extracted)
    missing = [k for k, v in need.items() if not _contains(v, r.observed_text)]
    if missing:
        return Outcome(CheckStatus.MANUAL_REVIEW, r.confidence, expected, r.observed_text,
                       f"Not found on packing list: {', '.join(missing)} - QC to reconcile.", extracted)
    return Outcome(_ai_status(r), r.confidence, expected, r.observed_text,
                   f"PO details found on packing list. {r.reason}", extracted, ai_decided=True)


def _inner_pack(insp: Inspection, inp: CheckInputs, visual: Outcome) -> Outcome:
    """BRD check 15: the visual check plus the piece count against the PO packing norm."""
    norm = _reading((insp.po.packing_norms or {}).get("pcs_per_inner"))
    if norm is None:
        if visual.status == CheckStatus.FAIL:
            return visual
        visual.status = CheckStatus.MANUAL_REVIEW
        visual.reason = f"No pieces-per-inner norm on PO - QC to verify the quantity. {visual.reason}"
        return visual
    visual.expected = f"{visual.expected}, {norm:g} pcs per inner"
    if INNER_PACK_FIELD not in inp.measured:
        visual.status = CheckStatus.MANUAL_REVIEW
        visual.reason = f"Enter readings for: {INNER_PACK_FIELD}. {visual.reason}"
        return visual
    count = _reading(inp.measured[INNER_PACK_FIELD])
    if count is None:
        visual.status = CheckStatus.MANUAL_REVIEW
        visual.reason = f"Invalid reading for: {INNER_PACK_FIELD} - enter a number."
        return visual
    visual.extracted["measured"] = {INNER_PACK_FIELD: count}
    visual.observed = f"{count:g} pcs; {visual.observed}".rstrip("; ")
    if count != norm:  # a counted mismatch is a fact, not an AI judgement
        visual.status, visual.confidence, visual.ai_decided = CheckStatus.FAIL, 1.0, False
        visual.reason = f"Inner pack has {count:g} pcs, packing norm is {norm:g}."
        return visual
    visual.reason = f"Quantity {count:g} pcs as per norm. {visual.reason}"
    return visual


_VENDOR_TASKS = {
    "carton": "Read the carton markings (supplier name / code, PO number, article, quantity) and inspect the carton "
              "condition: damage, crushing, moisture marks and visible packing defects.",
    "pocketing": "Read the supplier name / code and batch on the pocketing tag and inspect the visible pocketing fabric.",
    "zip": "Read the zip puller / tape branding (supplier) and inspect zip type, color, length and whether it runs smoothly.",
    "polybag": "Read the poly bag supplier print and markings and inspect the bag specification and condition.",
}


def _nominated_vendor(db: Session, insp: Inspection, check: CheckDef, frame: bytes, inp: CheckInputs, q: dict) -> Outcome:
    po = insp.po
    nominated = db.scalars(select(NominatedVendor).where(NominatedVendor.category == check.vendor_category)).all()
    allowed = {_norm(v.supplier_code): v.supplier_name for v in nominated}
    expected = ", ".join(f"{v.supplier_name} ({v.supplier_code})" for v in nominated) or "no nominated vendor configured"
    task = _VENDOR_TASKS.get(check.vendor_category or "", "Read the supplier name / code / batch printed on the item "
                                                           "and check visible quality and damage.")
    r = get_provider().analyze(VisionTask(check.title, task, frame))
    supplier = inp.supplier_code or r.observed_text
    batch = str(inp.batch_no or "").strip()[:64]
    extracted = {"ai": r.__dict__, "supplier": supplier, "batch_no": batch, **q}
    observed = supplier + (f", batch {batch}" if batch else "")
    if not nominated:
        return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, observed, "Nominated vendor master is empty.", extracted)
    if not supplier:
        return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, "", "Supplier not identified - enter supplier code.", extracted)
    if _norm(supplier) not in allowed:
        # a typed supplier code is a fact; a name only the AI read off the item is its judgement
        return Outcome(CheckStatus.FAIL, 1.0 if inp.supplier_code else r.confidence, expected, observed,
                       "Supplier is not a nominated vendor.", extracted, ai_decided=not inp.supplier_code)
    name = allowed[_norm(supplier)]
    status = _ai_status(r)

    if check.vendor_category == "carton":
        # BRD 4D: grade, dimensions and board strength cannot be established from video, so the carton never
        # passes automatically. A visible defect can still fail it.
        marks = {"PO": po.po_number, "article": po.article_number}
        missing = [k for k, v in marks.items() if not _contains(v, r.observed_text)]
        marks_note = f"Markings missing: {', '.join(missing)}." if missing else "PO and article markings found."
        if status == CheckStatus.FAIL:
            return Outcome(status, r.confidence, expected, observed, f"Nominated supplier {name}. {r.reason}",
                           extracted, ai_decided=True)
        return Outcome(CheckStatus.MANUAL_REVIEW, r.confidence, expected, observed,
                       f"Nominated supplier {name}. {marks_note} Carton grade, dimensions and board strength need QC "
                       f"confirmation or a test report. Visual: {r.reason}", extracted)

    if not batch:  # BRD 18-20: supplier *and batch* details are verified
        return Outcome(CheckStatus.MANUAL_REVIEW, r.confidence, expected, observed,
                       f"Nominated supplier {name}, but batch number not recorded - enter the batch number.", extracted)
    return Outcome(status, r.confidence, expected, observed, f"Nominated supplier {name}, batch {batch}. Visual: {r.reason}",
                   extracted, ai_decided=True)


def _reading(value) -> float | None:
    """A finite number, or None for NaN / infinity / booleans / text."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def measurement_targets(check: CheckDef, insp: Inspection) -> dict[str, tuple] | None:
    """{reading: (target, tolerance)} from the PO, or None when the PO has no usable target for this check."""
    po = insp.po
    if check.po_field == "weight":
        targets = {"weight_g": (po.weight_target_g, po.weight_tolerance_g)}
    elif check.po_field == "gsm":
        targets = {"gsm": (po.gsm_target, po.gsm_tolerance)}
    else:
        targets = {k: tuple(v) for k, v in (po.size_chart.get(insp.size) or {}).items()}
    if not targets or any(t is None or tol is None for t, tol in targets.values()):
        return None
    return targets


def required_inputs(insp: Inspection, check_no: int) -> list[str]:
    """Readings to ask the vendor for. None for a measurement the PO has no target for:
    the reading could not be checked, so asking (and waiting for it) is pointless."""
    check = BY_NO[check_no]
    if check.method == Method.MEASUREMENT and measurement_targets(check, insp) is None:
        return []
    if check_no == 15:  # inner pack: count the pieces when the PO has a packing norm
        return [INNER_PACK_FIELD] if _reading((insp.po.packing_norms or {}).get("pcs_per_inner")) is not None else []
    return input_fields(check_no, list((insp.po.size_chart.get(insp.size) or {}).keys()))


def _measurement(check: CheckDef, insp: Inspection, inp: CheckInputs, q: dict) -> Outcome:
    targets = measurement_targets(check, insp)
    if targets is None:
        return _manual("", "No target / tolerance on PO for this check - QC to verify.", quality=q)
    expected = ", ".join(f"{k} {t}±{tol}" for k, (t, tol) in targets.items())
    missing = [k for k in targets if k not in inp.measured]
    if missing:
        return _manual(expected, f"Enter readings for: {', '.join(missing)}.", quality=q)

    values = {k: _reading(inp.measured[k]) for k in targets}
    invalid = [k for k, v in values.items() if v is None]
    if invalid:  # NaN / infinity / text must never be compared (NaN would slip through as "in tolerance")
        return _manual(expected, f"Invalid reading for: {', '.join(invalid)} - enter a number.", quality=q)

    out = [k for k, (t, tol) in targets.items() if abs(values[k] - t) > tol]
    observed = ", ".join(f"{k} {values[k]:g}" for k in targets)
    extracted = {"measured": {k: values[k] for k in targets}, **q}
    if out:
        return Outcome(CheckStatus.FAIL, 1.0, expected, observed, f"Out of tolerance: {', '.join(out)}.", extracted)
    return Outcome(CheckStatus.PASS, 1.0, expected, observed, "All readings within tolerance.", extracted)
