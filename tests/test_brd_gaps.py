"""BRD gap fixes: checks 5, 6, 14, 15, 17-20, reference choice (4C), lighting (4A), AI auto-decide (section 10),
report (section 8). Each test builds its own data, so test order does not matter."""
import io
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import textured_jpeg
from openpyxl import load_workbook
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.checklist import BY_NO
from app.core.config import settings
from app.core.database import Base
from app.models import CheckStatus, NominatedVendor, PurchaseOrder, ReferenceAsset
from app.services import imaging, report, verification
from app.services.ai_vision import AIResult
from app.services.verification import CheckInputs, run_check

BAR, TAF = "8901234567890", "TF-7788-M"


def _po(**kw):
    base = dict(color_name="NAVY BLUE", mc="MC-40", barcode_number=BAR, taffeta_number=TAF, weight_target_g=210,
                weight_tolerance_g=10, gsm_target=None, gsm_tolerance=None, size_chart={}, po_number="PO-1001",
                asn_number="ASN-5001", article_number="ART-77", style_number="ST-9", order_qty=1200,
                packing_norms={"pcs_per_inner": 5})
    return SimpleNamespace(**(base | kw))


def _insp(**kw):
    return SimpleNamespace(po=_po(**kw), po_id=1, size="M", color_name="NAVY BLUE")


class _AI:
    def __init__(self, text="", verdict="pass", confidence=0.95):
        self.r = AIResult(verdict, confidence, text, "looks right", [])
        self.tasks = []

    def analyze(self, task):
        self.tasks.append(task)
        return self.r


@pytest.fixture
def ai(monkeypatch):
    """Install a fake AI that reads `text`; returns a setter."""
    def use(text="", verdict="pass", confidence=0.95):
        fake = _AI(text, verdict, confidence)
        monkeypatch.setattr(verification, "get_provider", lambda: fake)
        return fake
    monkeypatch.setattr(verification.barcode, "decode", lambda frame: [])
    return use


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(settings, "ai_auto_decide", True)


def _run(no, insp=None, db=None, **inputs):
    return run_check(db, insp or _insp(), BY_NO[no], textured_jpeg(), CheckInputs(**inputs))


# ---------- check 6 / BRD 4B ----------

def test_check6_article_found_on_labels_passes(ai):
    ai(f"ART-77 / ST-9 {TAF} NAVY BLUE M")
    out = _run(6, codes=[BAR, TAF])
    assert out.status == CheckStatus.PASS and "found on labels" in out.reason


def test_check6_readable_labels_without_article_go_to_qc(ai):
    ai("ART-11 / ST-2 NAVY BLUE M")
    out = _run(6, codes=[BAR, TAF])
    assert out.status == CheckStatus.MANUAL_REVIEW and "not found on the labels" in out.reason


def test_check6_extra_or_duplicate_code_is_flagged(ai):
    ai("")
    out = _run(6, codes=[BAR, TAF, "8909999999999"])
    assert out.status == CheckStatus.MANUAL_REVIEW and "Extra / duplicate" in out.reason


def test_check6_only_taffeta_seen_is_unable_to_read_not_fail(ai):
    ai("")
    out = _run(6, codes=[TAF])
    assert out.status == CheckStatus.MANUAL_REVIEW and out.reason.startswith("Unable to read")


def test_check6_wrong_barcode_still_fails(ai):
    ai("")
    assert _run(6, codes=["111"]).status == CheckStatus.FAIL


def test_check6_codes_match_but_ai_unavailable_passes_on_barcode(ai):
    ai("", verdict="unsure", confidence=0.0)
    out = _run(6, codes=[BAR, TAF])
    assert out.status == CheckStatus.PASS and "identified by the barcode" in out.reason


# ---------- check 14 ----------

def test_packing_list_must_show_size_and_quantity(ai, auto):
    ai("PO-1001 ASN-5001 ART-77 NAVY BLUE S 300 L 300")
    out = _run(14)
    assert out.status == CheckStatus.MANUAL_REVIEW and "size" in out.reason and "quantity" in out.reason
    ai("PO-1001 ASN-5001 ART-77 NAVY BLUE M 1200")
    assert _run(14).status == CheckStatus.PASS


# ---------- check 15 ----------

def _with_ref(monkeypatch, ref=b"ref"):
    monkeypatch.setattr(verification, "_reference", lambda db, insp, kind: ref)


def test_inner_pack_wrong_count_fails_even_without_reference(ai, monkeypatch):
    ai("ok")
    _with_ref(monkeypatch, None)
    out = _run(15, measured={"inner_pack_pcs": 4})
    assert out.status == CheckStatus.FAIL and "4 pcs" in out.reason


def test_inner_pack_right_count_and_visual_ok_passes(ai, auto, monkeypatch):
    ai("ok")
    _with_ref(monkeypatch)
    out = _run(15, measured={"inner_pack_pcs": 5})
    assert out.status == CheckStatus.PASS and "Quantity 5 pcs" in out.reason


def test_inner_pack_needs_count_and_norm(ai, auto, monkeypatch):
    ai("ok")
    _with_ref(monkeypatch)
    assert _run(15).status == CheckStatus.MANUAL_REVIEW                                  # count not entered
    out = _run(15, insp=_insp(packing_norms={}), measured={"inner_pack_pcs": 5})
    assert out.status == CheckStatus.MANUAL_REVIEW and "norm" in out.reason              # no norm on PO
    assert verification.required_inputs(_insp(), 15) == ["inner_pack_pcs"]
    assert verification.required_inputs(_insp(packing_norms={}), 15) == []


# ---------- checks 17-20 ----------

def _vendor_db(category):
    rows = [NominatedVendor(category=category, supplier_code="SUP-1", supplier_name="Good Supplier")]
    return SimpleNamespace(scalars=lambda q: SimpleNamespace(all=lambda: rows))


def test_carton_never_passes_automatically(ai, auto):
    ai("PO-1001 ART-77 SUP-1")
    out = _run(17, db=_vendor_db("carton"), supplier_code="SUP-1")
    assert out.status == CheckStatus.MANUAL_REVIEW and "board strength" in out.reason
    assert "PO and article markings found" in out.reason
    ai("crushed corner", verdict="fail")
    assert _run(17, db=_vendor_db("carton"), supplier_code="SUP-1").status == CheckStatus.FAIL


@pytest.mark.parametrize("no,category", [(18, "pocketing"), (19, "zip"), (20, "polybag")])
def test_supplier_and_batch_required(ai, auto, no, category):
    ai("ok")
    out = _run(no, db=_vendor_db(category), supplier_code="SUP-1")
    assert out.status == CheckStatus.MANUAL_REVIEW and "batch number" in out.reason
    out = _run(no, db=_vendor_db(category), supplier_code="SUP-1", batch_no="B-42")
    assert out.status == CheckStatus.PASS and "batch B-42" in out.reason and out.extracted["batch_no"] == "B-42"


def test_supplier_name_read_only_by_ai_does_not_auto_fail(ai):
    ai("Some Other Mills")
    out = _run(18, db=_vendor_db("pocketing"))
    assert out.status == CheckStatus.MANUAL_REVIEW and "AI suggests FAIL" in out.reason
    assert _run(18, db=_vendor_db("pocketing"), supplier_code="NOPE").status == CheckStatus.FAIL  # typed code: a fact


# ---------- check 5 ----------

def test_size_label_needs_approved_label_standard(ai, auto, monkeypatch):
    ai("SIZE M")
    _with_ref(monkeypatch, None)
    out = _run(5)
    assert out.status == CheckStatus.MANUAL_REVIEW and "size_label" in out.reason
    _with_ref(monkeypatch)
    fake = ai("SIZE M")
    assert _run(5).status == CheckStatus.PASS and fake.tasks[1].reference == b"ref"


# ---------- reference choice / BRD 4C ----------

def test_reference_matches_color_and_size_never_another_color(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(verification.storage, "read_file", lambda path: path.encode())
    with Session(engine) as db:
        po = PurchaseOrder(po_number="P", vendor_code="V", article_number="A", style_number="S", color_name="NAVY",
                           mc="M", barcode_number="1", taffeta_number="2")
        db.add(po)
        db.flush()
        add = lambda path, color=None, size=None: db.add(ReferenceAsset(po_id=po.id, kind="pps", color_name=color,
                                                                         size=size, file_path=path))
        add("red", "RED")
        db.commit()
        insp = SimpleNamespace(po_id=po.id, color_name="navy", size="M")
        assert verification._reference(db, insp, "pps") is None          # red PPS is never used for navy
        add("generic")
        add("navy-L", "NAVY", "L")
        add("navy", "NAVY")
        add("navy-M", "NAVY", "M")
        db.commit()
        assert verification._reference(db, insp, "pps") == b"navy-M"
        assert {r.file_path for r in verification.applicable_references(db, insp)} == {"generic", "navy", "navy-M"}


# ---------- lighting / BRD 4A ----------

def _glare_jpeg() -> bytes:
    arr = np.asarray(Image.open(io.BytesIO(textured_jpeg()))).copy()
    arr[:, :120] = 255   # 30% blown-out white
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_glare_sends_color_check_back_for_reshow():
    assert imaging.lighting_issues(textured_jpeg()) == []
    assert imaging.lighting_issues(_glare_jpeg())
    out = run_check(None, _insp(), BY_NO[1], _glare_jpeg(), CheckInputs())
    assert out.status == CheckStatus.MANUAL_REVIEW and out.reason.startswith("Reshow")


# ---------- AI auto-decide / BRD section 10 ----------

def test_ai_decision_is_only_a_suggestion_until_pilot_validated(ai, monkeypatch):
    assert settings.ai_auto_decide is False                                # default
    ai("ok")
    _with_ref(monkeypatch)
    out = _run(7)
    assert out.status == CheckStatus.MANUAL_REVIEW and out.reason.startswith("AI suggests PASS")
    monkeypatch.setattr(settings, "ai_auto_decide", True)
    assert _run(7).status == CheckStatus.PASS


# ---------- report / BRD section 8 ----------

def _report_insp():
    now = datetime.now(timezone.utc)
    ev = SimpleNamespace(captured_at=now, file_path="missing.jpg",
                         extracted={"outcome": {"status": "fail", "reason": "Out of tolerance ± 5"}})
    checks = [SimpleNamespace(check_no=n, status="pass", ai_status="pass", confidence=0.9, expected_value="210±10 – g",
                              observed_value="ok", reason="ok", reviewer_id=7 if n == 2 else None,
                              reviewer_comment="fine", corrective_action="", evidence=[ev] if n == 8 else [])
              for n in range(1, 21)]
    return SimpleNamespace(id=5, created_at=now, decided_at=now, decided_by_id=9, exception_recorded=False,
                           color_name="NAVY", size="M", status="passed", decision_note="ok ≤ 3", checks=checks,
                           po=SimpleNamespace(vendor_code="V1", po_number="PO-1", asn_number="ASN-1", article_number="A"))


def test_report_has_reviewer_approver_closure_and_references():
    insp = _report_insp()
    extras = report.ReportExtras(names={7: "QC Person", 9: "Approver Person"},
                                 references=[report.Reference("pps", "NAVY", None, "approved", "missing.jpg")])
    wb = load_workbook(io.BytesIO(report.build_excel(insp, [insp], extras)))
    values = {c.value for ws in wb for row in ws.iter_rows() for c in row}
    assert {"QC Person", "Approver Person", "Closure date", "pps"} <= values
    assert "Approved references" in wb.sheetnames

    pdf = report.build_pdf(insp, [insp], extras)
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000
