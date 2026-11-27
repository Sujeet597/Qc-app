"""The 20 garment QC checklist points from BRD section 3, with how each one is verified."""
from dataclasses import dataclass
from enum import StrEnum


class Method(StrEnum):
    COLOR_COMPARE = "color_compare"        # Delta-E vs PPS reference image
    OCR_MATCH = "ocr_match"                # AI reads text from label, code compares with PO field
    BARCODE_MATCH = "barcode_match"        # decoded barcode / taffeta values vs PO
    VISUAL_COMPARE = "visual_compare"      # AI compares live frame vs sealer reference
    DEFECT_SCAN = "defect_scan"            # AI looks for visible defects (no reference needed)
    MEASUREMENT = "measurement"            # value entered from scale / tape / GSM cutter vs tolerance
    DOCUMENT_RECONCILE = "document_reconcile"  # packing list read and reconciled with PO
    VENDOR_MASTER = "vendor_master"        # supplier code vs nominated vendor master


@dataclass(frozen=True)
class CheckDef:
    no: int
    title: str
    method: Method
    critical: bool
    instruction: str                      # what the vendor is told to show on camera
    reference_kind: str | None = None     # which ReferenceAsset.kind to compare against
    po_field: str | None = None           # PO field for OCR / measurement checks
    vendor_category: str | None = None    # nominated vendor category


CHECKLIST: tuple[CheckDef, ...] = (
    CheckDef(1, "Garment color vs PPS", Method.COLOR_COMPARE, True,
             "Hold the garment flat under the daylight lamp, next to the color reference card, filling the frame.",
             reference_kind="pps"),
    CheckDef(2, "Barcode color name vs PO color name", Method.OCR_MATCH, True,
             "Show the barcode sticker close up so the color name is readable.", po_field="color_name"),
    CheckDef(3, "Actual MC vs PO MC", Method.OCR_MATCH, False,
             "Show the master carton marking / MC details close up.", po_field="mc"),
    CheckDef(4, "Main label", Method.VISUAL_COMPARE, False,
             "Show the main label straight on, then its placement on the garment.", reference_kind="main_label"),
    CheckDef(5, "Size label", Method.OCR_MATCH, False,
             "Show the size label close up.", po_field="size", reference_kind="size_label"),
    CheckDef(6, "Barcode / taffeta number as per PO", Method.BARCODE_MATCH, True,
             "Show the barcode label and the taffeta label one after the other, close up."),
    CheckDef(7, "Trims as per sealer", Method.VISUAL_COMPARE, False,
             "Show buttons, trims and accessories close up.", reference_kind="sealer"),
    CheckDef(8, "Weight as per sealer", Method.MEASUREMENT, False,
             "Place the garment on the scale, keep the scale display clearly visible on camera and enter the reading.", po_field="weight"),
    CheckDef(9, "Measurements as per norms", Method.MEASUREMENT, False,
             "Measure each point of measure with the tape on camera and enter the values.", po_field="size_chart"),
    CheckDef(10, "GSM as per costing", Method.MEASUREMENT, False,
             "Cut a GSM sample, weigh it with the scale / GSM tester display clearly visible on camera and enter the reading.", po_field="gsm"),
    CheckDef(11, "Print as per sealer", Method.VISUAL_COMPARE, True,
             "Show the full print, then a close up.", reference_kind="sealer"),
    CheckDef(12, "Fabric as per sealer", Method.VISUAL_COMPARE, False,
             "Show the fabric surface close up under the lamp.", reference_kind="sealer"),
    CheckDef(13, "Embroidery / applique as per sealer", Method.VISUAL_COMPARE, False,
             "Show the embroidery / applique, front and close up.", reference_kind="sealer"),
    CheckDef(14, "Packing list", Method.DOCUMENT_RECONCILE, False,
             "Show the packing list flat so all rows are readable."),
    CheckDef(15, "Inner pack", Method.VISUAL_COMPARE, False,
             "Open one inner pack and show contents and labels.", reference_kind="packing"),
    CheckDef(16, "Ironing", Method.DEFECT_SCAN, False,
             "Show the garment front and back fully, flat, under the lamp."),
    CheckDef(17, "Carton nominated vendor", Method.VENDOR_MASTER, False,
             "Show the carton supplier marking and the carton condition.", vendor_category="carton"),
    CheckDef(18, "Pocketing nominated vendor", Method.VENDOR_MASTER, False,
             "Show the pocketing fabric and its supplier / batch tag close up.", vendor_category="pocketing"),
    CheckDef(19, "Zip nominated vendor", Method.VENDOR_MASTER, False,
             "Show the zip puller branding, run the zip on camera.", vendor_category="zip"),
    CheckDef(20, "Poly bag nominated vendor", Method.VENDOR_MASTER, False,
             "Show the poly bag markings and supplier print.", vendor_category="polybag"),
)

BY_NO: dict[int, CheckDef] = {c.no: c for c in CHECKLIST}


def input_fields(no: int, size_chart_keys: list[str]) -> list[str]:
    """Readings the vendor must type for a check (scale, tape, GSM, supplier). Empty = camera only."""
    d = BY_NO[no]
    if d.method == Method.VENDOR_MASTER:
        return ["supplier_code"]
    if d.method != Method.MEASUREMENT:
        return []
    if d.po_field == "weight":
        return ["weight_g"]
    if d.po_field == "gsm":
        return ["gsm"]
    return size_chart_keys
