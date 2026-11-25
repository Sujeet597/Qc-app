"""Runs one checklist point against one captured frame. BRD section 3 + section 4 special checks."""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.checklist import CheckDef, Method
from app.core.config import settings
from app.models import CheckStatus, Inspection, NominatedVendor, ReferenceAsset
from app.services import barcode, imaging, storage
from app.services.ai_vision import VisionTask, get_provider
from app.services.decision import from_ai


@dataclass
class CheckInputs:
    codes: list[str] = field(default_factory=list)       # barcodes decoded in the browser (zxing-wasm)
    measured: dict = field(default_factory=dict)         # {"weight_g": 212} / {"gsm": 180} / {"chest_cm": 52.5}
    supplier_code: str | None = None                     # for nominated-vendor checks


@dataclass
class Outcome:
    status: CheckStatus
    confidence: float | None
    expected: str
    observed: str
    reason: str
    extracted: dict = field(default_factory=dict)


def _norm(s: str) -> str:
    return "".join(s.split()).casefold()


def _reference(db: Session, insp: Inspection, kind: str) -> bytes | None:
    rows = db.scalars(
        select(ReferenceAsset).where(ReferenceAsset.po_id == insp.po_id, ReferenceAsset.kind == kind)
        .order_by(ReferenceAsset.uploaded_at.desc())
    ).all()
    # prefer a reference for the same color, then any
    best = next((r for r in rows if r.color_name and _norm(r.color_name) == _norm(insp.color_name)), None) or (
        rows[0] if rows else None)
    return storage.read_file(best.file_path) if best else None


def _manual(expected: str, reason: str, **extra) -> Outcome:
    return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, "", reason, extra)


def run_check(db: Session, insp: Inspection, check: CheckDef, frame: bytes, inp: CheckInputs) -> Outcome:
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
            expected = insp.size if check.po_field == "size" else str(getattr(po, check.po_field or "", ""))
            label = {"size": "size", "color_name": "color name", "mc": "master carton (MC) details"}.get(check.po_field, check.po_field)
            r = ai.analyze(VisionTask(check.title, f"Read the {label} exactly as printed. Put it in observed_text.", frame))
            extracted = {"ai": r.__dict__, **q}
            if r.verdict == "unsure" or not r.observed_text:
                return Outcome(CheckStatus.MANUAL_REVIEW, r.confidence, expected, r.observed_text, r.reason, extracted)
            if _norm(expected) in _norm(r.observed_text):
                return Outcome(from_ai("pass", r.confidence), r.confidence, expected, r.observed_text, "Matches PO.", extracted)
            return Outcome(from_ai("fail", r.confidence), r.confidence, expected, r.observed_text,
                           f"Label reads '{r.observed_text}', PO says '{expected}'.", extracted)

        case Method.BARCODE_MATCH:
            codes = list(dict.fromkeys(inp.codes + barcode.decode(frame)))
            expected = f"barcode {po.barcode_number}, taffeta {po.taffeta_number}"
            found_bar = po.barcode_number in codes
            found_taf = po.taffeta_number in codes
            extracted = {"codes": codes, **q}
            if found_bar and not found_taf:  # taffeta is often printed text, not a barcode
                r = ai.analyze(VisionTask(check.title, "Read the taffeta label number exactly as printed.", frame))
                extracted["ai"] = r.__dict__
                found_taf = bool(r.observed_text) and _norm(po.taffeta_number) in _norm(r.observed_text)
            if found_bar and found_taf:
                return Outcome(CheckStatus.PASS, 1.0, expected, ", ".join(codes), "Barcode and taffeta match PO.", extracted)
            if not codes:
                return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, "", "Unable to read - show labels closer.", extracted)
            if not found_bar:
                return Outcome(CheckStatus.FAIL, 1.0, expected, ", ".join(codes), "Barcode does not match PO.", extracted)
            return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, ", ".join(codes),
                           "Barcode OK, taffeta number not confirmed.", extracted)

        case Method.VISUAL_COMPARE | Method.DEFECT_SCAN:
            ref = None
            if check.method == Method.VISUAL_COMPARE:
                ref = _reference(db, insp, check.reference_kind or "sealer") or _reference(db, insp, "sealer")
                if ref is None:
                    return _manual("Approved sealer", f"No '{check.reference_kind}' reference uploaded.", quality=q)
                task = f"Compare the live frame with the approved reference for: {check.title}. Check design, placement, color, size and visible defects."
            else:
                task = "Inspect for creases, shine marks, burns, stains and other visible ironing / finishing defects."
            r = ai.analyze(VisionTask(check.title, task, frame, ref))
            obs = r.observed_text or "; ".join(r.defects)
            return Outcome(from_ai(r.verdict, r.confidence), r.confidence, "As per sealer" if ref else "No visible defects",
                           obs, r.reason, {"ai": r.__dict__, **q})

        case Method.DOCUMENT_RECONCILE:
            need = {"PO": po.po_number, "article": po.article_number, "color": insp.color_name}
            r = ai.analyze(VisionTask(check.title, "Read this packing list. Copy the PO number, article, color, sizes and quantities into observed_text.",
                                      frame, context=need))
            missing = [k for k, v in need.items() if _norm(v) not in _norm(r.observed_text)]
            expected = ", ".join(f"{k} {v}" for k, v in need.items())
            if r.verdict == "unsure" or missing:
                reason = r.reason if r.verdict == "unsure" else f"Not found on packing list: {', '.join(missing)}."
                return Outcome(CheckStatus.MANUAL_REVIEW, r.confidence, expected, r.observed_text, reason, {"ai": r.__dict__, **q})
            return Outcome(from_ai(r.verdict, r.confidence), r.confidence, expected, r.observed_text, r.reason, {"ai": r.__dict__, **q})

        case Method.MEASUREMENT:
            return _measurement(check, insp, inp, q)

        case Method.VENDOR_MASTER:
            nominated = db.scalars(select(NominatedVendor).where(NominatedVendor.category == check.vendor_category)).all()
            allowed = {_norm(v.supplier_code): v.supplier_name for v in nominated}
            expected = ", ".join(f"{v.supplier_name} ({v.supplier_code})" for v in nominated) or "no nominated vendor configured"
            r = ai.analyze(VisionTask(check.title, "Read the supplier name / code / batch printed on the item and check visible quality and damage.", frame))
            supplier = inp.supplier_code or r.observed_text
            extracted = {"ai": r.__dict__, "supplier": supplier, **q}
            if not nominated:
                return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, supplier, "Nominated vendor master is empty.", extracted)
            if not supplier:
                return Outcome(CheckStatus.MANUAL_REVIEW, None, expected, "", "Supplier not identified - enter supplier code.", extracted)
            if _norm(supplier) not in allowed:
                return Outcome(CheckStatus.FAIL, 1.0, expected, supplier, "Supplier is not a nominated vendor.", extracted)
            status = from_ai(r.verdict, r.confidence)
            return Outcome(status, r.confidence, expected, supplier,
                           f"Nominated supplier {allowed[_norm(supplier)]}. Visual: {r.reason}", extracted)

    raise ValueError(f"Unhandled method {check.method}")


def _measurement(check: CheckDef, insp: Inspection, inp: CheckInputs, q: dict) -> Outcome:
    po = insp.po
    if check.po_field == "weight":
        targets = {"weight_g": (po.weight_target_g, po.weight_tolerance_g)}
    elif check.po_field == "gsm":
        targets = {"gsm": (po.gsm_target, po.gsm_tolerance)}
    else:
        targets = {k: tuple(v) for k, v in (po.size_chart.get(insp.size) or {}).items()}

    if not targets or any(t is None or tol is None for t, tol in targets.values()):
        return _manual("", "No target / tolerance on PO for this check - QC to verify.", quality=q)
    expected = ", ".join(f"{k} {t}±{tol}" for k, (t, tol) in targets.items())
    missing = [k for k in targets if k not in inp.measured]
    if missing:
        return _manual(expected, f"Enter readings for: {', '.join(missing)}.", quality=q)

    out = [k for k, (t, tol) in targets.items() if abs(float(inp.measured[k]) - t) > tol]
    observed = ", ".join(f"{k} {inp.measured[k]}" for k in targets)
    extracted = {"measured": inp.measured, **q}
    if out:
        return Outcome(CheckStatus.FAIL, 1.0, expected, observed, f"Out of tolerance: {', '.join(out)}.", extracted)
    return Outcome(CheckStatus.PASS, 1.0, expected, observed, "All readings within tolerance.", extracted)
