"""Senior's decisions of 2026-11-27: safe autopilot, supplier / batch from the database (checks 17-20),
packing list vs ASN quantity (check 14), weight / GSM display cross-check (checks 8, 10).
Each test builds its own data, so test order does not matter."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from conftest import login, textured_jpeg
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.checklist import BY_NO
from app.core.config import settings
from app.core.database import Base, SessionLocal
from app.models import CheckResult, CheckStatus, Inspection, InspectionStatus, NominatedVendor, PurchaseOrder, User
from app.services import autopilot, verification
from app.services.ai_vision import AIResult
from app.services.verification import CheckInputs, required_inputs, run_check

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


# ---------- autopilot ----------

@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _submitted(db, statuses=None, hours_ago=30, reviewed_after_submit=False):
    vendor = User(username=f"v{uuid.uuid4().hex[:6]}", password_hash="x", role="vendor", vendor_code="V1")
    po = PurchaseOrder(po_number=f"P{uuid.uuid4().hex[:6]}", vendor_code="V1", article_number="A", style_number="S",
                       color_name="RED", mc="M", barcode_number="1", taffeta_number="2", sizes=["M"])
    db.add_all([vendor, po])
    db.flush()
    submitted = NOW - timedelta(hours=hours_ago)
    statuses = statuses or {}
    checks = [CheckResult(check_no=n, status=statuses.get(n, CheckStatus.PASS), updated_at=submitted - timedelta(hours=1))
              for n in range(1, 21)]
    if reviewed_after_submit:
        checks[0].reviewer_id, checks[0].updated_at = vendor.id, submitted + timedelta(hours=1)
    insp = Inspection(po_id=po.id, vendor_user_id=vendor.id, color_name="RED", size="M",
                      status=InspectionStatus.SUBMITTED, submitted_at=submitted, checks=checks)
    db.add(insp)
    db.commit()
    return insp


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(settings, "autopilot_wait_hours", 24)


def test_autopilot_is_off_by_default(db):
    assert settings.autopilot_enabled is False
    insp = _submitted(db)
    assert autopilot.run_once(db, NOW) == [] and insp.status == InspectionStatus.SUBMITTED


def test_autopilot_approves_only_clean_inspections_after_waiting(db, on):
    clean = _submitted(db)
    too_early = _submitted(db, hours_ago=2)
    review = _submitted(db, statuses={12: CheckStatus.MANUAL_REVIEW})
    failed = _submitted(db, statuses={8: CheckStatus.FAIL})
    critical = _submitted(db, statuses={6: CheckStatus.FAIL})
    qc_busy = _submitted(db, reviewed_after_submit=True)

    assert autopilot.run_once(db, NOW) == [clean.id]
    assert clean.status == InspectionStatus.PASSED and clean.decided_by_id is None
    assert autopilot.is_auto_approved(clean) and clean.decision_note.startswith(autopilot.AUTO_NOTE)
    for insp in (too_early, review, failed, critical, qc_busy):
        assert insp.status == InspectionStatus.SUBMITTED
    assert autopilot.why_not(review, NOW).startswith("not all checks passed")
    assert autopilot.run_once(db, NOW) == []   # running again changes nothing


def test_autopilot_api_run_report_dashboard_and_reopen(client, monkeypatch):
    admin, qc = login(client, "admin", "admin123"), login(client, "qc", "qc123")
    assert client.post("/api/inspections/autopilot/run", headers=admin).status_code == 409   # off
    with SessionLocal() as s:
        iid = _submitted(s).id
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(settings, "autopilot_wait_hours", 24)
    assert client.post("/api/inspections/autopilot/run", headers=qc).status_code == 403
    assert iid in client.post("/api/inspections/autopilot/run", headers=admin).json()["approved"]
    assert client.get("/api/dashboard/summary", headers=qc).json()["auto_approved"] >= 1
    from io import BytesIO

    from openpyxl import load_workbook
    sheet = load_workbook(BytesIO(client.get(f"/api/inspections/{iid}/report.xlsx", headers=qc).content))["QC Report"]
    assert "System (autopilot)" in [c.value for row in sheet.iter_rows() for c in row]
    r = client.post(f"/api/inspections/{iid}/reopen", headers=qc)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    assert client.post(f"/api/inspections/{iid}/reopen", headers=qc).status_code == 409


# ---------- checks 17-20: supplier / batch from the database ----------

def _po(**kw):
    base = dict(po_number="PO-1", asn_number="ASN-1", article_number="ART-1", style_number="ST-1", order_qty=1000,
                weight_target_g=200, weight_tolerance_g=10, gsm_target=180, gsm_tolerance=8, size_chart={},
                packing_norms={}, suppliers=[], shipments=[])
    return SimpleNamespace(**(base | kw))


def _insp(**kw):
    return SimpleNamespace(po=_po(**kw), po_id=1, size="M", color_name="NAVY")


def _lot(category, code="SUP-1", batch="B-77"):
    return SimpleNamespace(category=category, supplier_code=code, batch_no=batch)


def _vendor_db(category, code="SUP-1"):
    rows = [NominatedVendor(category=category, supplier_code=code, supplier_name="Good Supplier")]
    return SimpleNamespace(scalars=lambda q: SimpleNamespace(all=lambda: rows))


@pytest.fixture
def ai(monkeypatch):
    def use(text="", verdict="pass", confidence=0.95):
        fake = SimpleNamespace(analyze=lambda task: AIResult(verdict, confidence, text, "looks right", []))
        monkeypatch.setattr(verification, "get_provider", lambda: fake)
    return use


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(settings, "ai_auto_decide", True)


def _run(no, insp, db=None, **inputs):
    return run_check(db, insp, BY_NO[no], textured_jpeg(), CheckInputs(**inputs))


def test_supplier_and_batch_compared_with_database(ai, auto):
    insp = _insp(suppliers=[_lot("zip")])
    ai("YKK SUP-1 BATCH B-77")
    out = _run(19, insp, _vendor_db("zip"), supplier_code="TYPED-OTHER")   # typing is ignored when DB has a value
    assert out.status == CheckStatus.PASS and "Tag matches database" in out.reason
    ai("SUP-1 BATCH B-99")
    out = _run(19, insp, _vendor_db("zip"))
    assert out.status == CheckStatus.MANUAL_REVIEW and "does not match the database (batch)" in out.reason
    ai("", verdict="unsure", confidence=0.0)
    assert _run(19, insp, _vendor_db("zip")).reason.startswith("Unable to read")


def test_recorded_supplier_must_be_nominated(ai):
    ai("SUP-9 B-77")
    out = _run(18, _insp(suppliers=[_lot("pocketing", code="SUP-9")]), _vendor_db("pocketing"))
    assert out.status == CheckStatus.FAIL and "not a nominated vendor" in out.reason


def test_carton_from_database_still_needs_qc(ai, auto):
    ai("CARTON SUP-1 PO-1 ART-1")
    out = _run(17, _insp(suppliers=[_lot("carton", batch="")]), _vendor_db("carton"))
    assert out.status == CheckStatus.MANUAL_REVIEW and "board strength" in out.reason


def test_vendor_is_not_asked_to_type_when_database_has_supplier():
    assert required_inputs(_insp(suppliers=[_lot("zip")]), 19) == []
    assert required_inputs(_insp(), 19) == ["supplier_code"]   # nothing recorded yet: typed fallback


# ---------- check 14: packing list vs ASN quantity ----------

def test_packing_list_needs_asn_quantity(ai, auto):
    text = "PO-1 ASN-1 ART-1 NAVY M 500 TOTAL 500"
    ai(text)
    assert "ASN shipped quantity not available" in _run(14, _insp()).reason
    ok = _insp(shipments=[SimpleNamespace(asn_number="ASN-1", shipped_qty=500)])
    assert _run(14, ok).status == CheckStatus.PASS
    over = _insp(order_qty=400, shipments=[SimpleNamespace(asn_number="ASN-1", shipped_qty=500)])
    out = _run(14, over)
    assert out.status == CheckStatus.MANUAL_REVIEW and "more than PO quantity" in out.reason


# ---------- checks 8, 10: display cross-check ----------

def test_weight_display_cross_check(ai):
    ai("205 g")
    out = _run(8, _insp(), measured={"weight_g": 205})
    assert out.status == CheckStatus.PASS and "confirmed" in out.reason
    ai("250")
    out = _run(8, _insp(), measured={"weight_g": 205})
    assert out.status == CheckStatus.MANUAL_REVIEW and "display shows 250" in out.reason
    ai("", verdict="unsure", confidence=0.0)
    out = _run(10, _insp(), measured={"gsm": 182})
    assert out.status == CheckStatus.PASS and "not confirmed" in out.reason   # unreadable display never changes the result


# ---------- admin APIs for PO supplier / ASN data ----------

def test_po_supplier_and_asn_api(client):
    admin, qc, vendor = login(client, "admin", "admin123"), login(client, "qc", "qc123"), login(client, "vendor1", "vendor123")
    po = client.post("/api/pos", headers=admin, json={
        "po_number": f"PO-{uuid.uuid4().hex[:6]}", "vendor_code": f"V-{uuid.uuid4().hex[:6]}", "article_number": "A", "style_number": "S",
        "color_name": "NAVY BLUE", "mc": "M", "barcode_number": "1", "taffeta_number": "2", "sizes": ["M"],
        "order_qty": 1000}).json()
    url = f"/api/pos/{po['id']}"
    assert client.put(f"{url}/suppliers/zip", headers=admin, json={"supplier_code": "YKK", "batch_no": "B1"}).status_code == 200
    assert client.put(f"{url}/suppliers/zip", headers=admin, json={"supplier_code": "YKK", "batch_no": "B2"}).json()["batch_no"] == "B2"
    assert client.put(f"{url}/suppliers/buttons", headers=admin, json={"supplier_code": "X"}).status_code == 422
    assert client.put(f"{url}/suppliers/zip", headers=qc, json={"supplier_code": "X"}).status_code == 403
    assert client.get(f"{url}/suppliers", headers=qc).json() == [{"category": "zip", "supplier_code": "YKK", "batch_no": "B2"}]
    assert client.get(f"{url}/suppliers", headers=vendor).status_code == 403
    r = client.put(f"{url}/asn", headers=admin, json={"asn_number": "ASN-9", "shipped_qty": 600})
    assert r.status_code == 200 and r.json()["asn_number"] == "ASN-9"
    assert client.delete(f"{url}/suppliers/zip", headers=admin).status_code == 204
    assert client.get(f"{url}/suppliers", headers=qc).json() == []
