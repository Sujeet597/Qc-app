"""Regression tests for the 8 logic bug fixes. Each test builds its own isolated DB / PO data."""
import asyncio
import io
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import textured_jpeg

from app.agent.controller import AgentTiming, InspectionController
from app.agent.frames import FrameSampler
from app.checklist import BY_NO, CHECKLIST
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models import CheckResult, CheckStatus, Inspection, PurchaseOrder, Role, User
from app.schemas import POCreate
from app.services import imaging, report, storage, verification
from app.services.ai_vision import AIResult
from app.services.evidence import record_evidence
from app.services.verification import CheckInputs, _measurement, measurement_targets, run_check


def _isolated_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False), engine


def _seed_inspection(db: Session, *, po_number="PO-ISO-1", size="M", size_chart=None, weight_target=210.0,
                     weight_tol=10.0, barcode="8901234567890", taffeta="TF-7788-M"):
    vendor = User(username=f"v_{po_number}", full_name="Vendor", password_hash="x", role=Role.VENDOR, vendor_code="V100")
    qc = User(username=f"qc_{po_number}", full_name="QC", password_hash="x", role=Role.QC)
    po = PurchaseOrder(
        po_number=po_number, asn_number="ASN-1", vendor_code="V100", article_number="ART-1",
        style_number="ST-1", color_name="NAVY BLUE", mc="MC-1", barcode_number=barcode,
        taffeta_number=taffeta, sizes=["S", "M", "L"], weight_target_g=weight_target,
        weight_tolerance_g=weight_tol, size_chart=size_chart or {},
    )
    db.add_all([vendor, qc, po])
    db.flush()
    insp = Inspection(po_id=po.id, vendor_user_id=vendor.id, color_name="NAVY BLUE", size=size,
                      checks=[CheckResult(check_no=c.no) for c in CHECKLIST])
    db.add(insp)
    db.commit()
    return insp, vendor, qc


def _jpeg_bytes(size=(200, 200), rgb=(40, 60, 110), seed=0) -> bytes:
    rng = np.random.default_rng(seed)
    w, h = size
    arr = np.clip(np.array(rgb) + rng.normal(0, 25, (h, w, 3)), 0, 255).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _png_bytes(size=(2, 2), rgb=(40, 60, 110)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="PNG")
    return buf.getvalue()


# 1. Second re-capture after QC Fail must stay Manual Review until QC reviews again
def test_second_recapture_after_qc_fail_stays_manual_review(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    SessionLocal, engine = _isolated_session(tmp_path)
    try:
        with SessionLocal() as db:
            insp, _, qc = _seed_inspection(db, po_number="PO-BUG1")
            frame = _jpeg_bytes()

            chk = record_evidence(db, insp, 8, frame, CheckInputs(measured={"weight_g": 300}))
            assert chk.status == CheckStatus.FAIL

            chk.status = CheckStatus.FAIL
            chk.reviewer_id = qc.id
            chk.reviewer_comment = "Too heavy"
            db.commit()

            # 1st re-capture with valid reading -> Manual Review
            chk = record_evidence(db, insp, 8, frame, CheckInputs(measured={"weight_g": 212}))
            assert chk.status == CheckStatus.MANUAL_REVIEW
            assert chk.reviewer_id is None

            # 2nd re-capture before QC reviews -> must still stay Manual Review
            chk = record_evidence(db, insp, 8, frame, CheckInputs(measured={"weight_g": 212}))
            assert chk.status == CheckStatus.MANUAL_REVIEW
            assert "QC failed this check earlier" in chk.reason

            # Once QC reviews as pass, a subsequent valid capture can pass
            chk.status = CheckStatus.PASS
            chk.reviewer_id = qc.id
            db.commit()
            chk = record_evidence(db, insp, 8, frame, CheckInputs(measured={"weight_g": 212}))
            assert chk.status == CheckStatus.PASS
    finally:
        engine.dispose()


# 2. Tiny images (< 100px) fail quality gate with finite scores and center_lab/delta_e are guarded
def test_tiny_image_rejected_by_quality_gate_and_delta_e_guarded(tmp_path):
    SessionLocal, engine = _isolated_session(tmp_path)
    try:
        tiny = _png_bytes((2, 2))
        q = imaging.assess_quality(tiny)
        assert not q.ok
        assert any("image too small" in issue for issue in q.issues)
        assert np.isfinite(q.sharpness) and np.isfinite(q.brightness)

        de = imaging.delta_e(tiny, _png_bytes((1, 1), (100, 120, 140)))
        assert np.isfinite(de)

        with SessionLocal() as db:
            insp, _, _ = _seed_inspection(db, po_number="PO-BUG2")
            out = run_check(db, insp, BY_NO[16], tiny, CheckInputs())
            assert out.status == CheckStatus.MANUAL_REVIEW
            assert "Reshow item: image too small" in out.reason
            assert np.isfinite(out.extracted["quality"]["sharpness"])
    finally:
        engine.dispose()


# 3. Float boundary in _measurement: 52.1 vs 52.0 +/- 0.1 must pass
def test_measurement_float_boundary_passes():
    po = SimpleNamespace(weight_target_g=None, weight_tolerance_g=None, gsm_target=None, gsm_tolerance=None,
                         size_chart={"M": {"chest_cm": [52.0, 0.1]}})
    insp = SimpleNamespace(po=po, size="M")
    assert _measurement(BY_NO[9], insp, CheckInputs(measured={"chest_cm": 52.1}), {}).status == CheckStatus.PASS
    assert _measurement(BY_NO[9], insp, CheckInputs(measured={"chest_cm": 51.9}), {}).status == CheckStatus.PASS
    assert _measurement(BY_NO[9], insp, CheckInputs(measured={"chest_cm": 52.11}), {}).status == CheckStatus.FAIL


# 4. POCreate rejects malformed size_chart; measurement_targets and GET /api/inspections handle legacy bad rows
def test_malformed_size_chart_rejected_and_get_inspections_safe(tmp_path, monkeypatch):
    base_po = dict(po_number="PO-BAD-SC", vendor_code="V100", article_number="A", style_number="S",
                   color_name="NAVY BLUE", mc="MC", barcode_number="1", taffeta_number="2", sizes=["M"])
    for bad in [{"M": {"chest_cm": [52]}}, {"M": {"chest_cm": [52, -0.5]}},
                {"M": {"chest_cm": [True, 1.0]}}, {"M": {"chest_cm": [float("nan"), 1.0]}}, {"M": "bad"}]:
        with pytest.raises(ValidationError):
            POCreate(**base_po, size_chart=bad)

    SessionLocal, engine = _isolated_session(tmp_path)
    try:
        with SessionLocal() as db:
            insp, vendor, _ = _seed_inspection(db, po_number="PO-BUG4", size_chart={"M": "not-a-dict"})
            assert measurement_targets(BY_NO[9], insp) is None

            from fastapi.testclient import TestClient

            def _db_override():
                with SessionLocal() as s:
                    yield s

            monkeypatch.setattr(settings, "auth_enabled", False)
            app.dependency_overrides[get_db] = _db_override
            try:
                with TestClient(app) as c:
                    r1 = c.get("/api/inspections", headers={"X-Role": "qc"})
                    r2 = c.get(f"/api/inspections/{insp.id}", headers={"X-Role": "qc"})
                    assert r1.status_code == 200 and r2.status_code == 200
            finally:
                app.dependency_overrides.clear()
    finally:
        engine.dispose()


# 5. Concurrent QC review while AI is running is preserved via db.refresh(chk)
def test_concurrent_qc_review_not_lost_during_run_check(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "ai_auto_decide", True)
    SessionLocal, engine = _isolated_session(tmp_path)
    try:
        with SessionLocal() as db:
            insp, _, qc = _seed_inspection(db, po_number="PO-BUG5")
            insp_id = insp.id

            class _SlowAI:
                def analyze(self, task):
                    # While AI call runs, QC marks the check FAIL in a separate DB session
                    with SessionLocal() as other:
                        other_insp = other.get(Inspection, insp_id)
                        other_chk = next(c for c in other_insp.checks if c.check_no == 16)
                        other_chk.status = CheckStatus.FAIL
                        other_chk.reviewer_id = qc.id
                        other_chk.reviewer_comment = "Scorch mark"
                        other.commit()
                    return AIResult("pass", 0.95, "", "Looks fine", [])

            monkeypatch.setattr(verification, "get_provider", lambda: _SlowAI())
            chk = record_evidence(db, insp, 16, _jpeg_bytes(), CheckInputs())
            assert chk.status == CheckStatus.MANUAL_REVIEW
            assert "QC failed this check earlier" in chk.reason
            assert chk.evidence[-1].extracted["replaced_review"]["comment"] == "Scorch mark"
    finally:
        engine.dispose()


# 6. _barcode_taffeta normalizes consistently and ignores taffeta text when AI verdict == "fail"
def test_barcode_taffeta_normalized_and_ignores_ai_fail(tmp_path, monkeypatch):
    SessionLocal, engine = _isolated_session(tmp_path)
    try:
        with SessionLocal() as db:
            insp, _, _ = _seed_inspection(db, po_number="PO-BUG6", barcode="BAR 123", taffeta="TF-7788")
            frame = _jpeg_bytes()

            # 6a: normalized codes match PO even with case / space differences
            monkeypatch.setattr(verification, "get_provider",
                                lambda: SimpleNamespace(analyze=lambda t: AIResult("pass", 0.95, "ART-1", "ok", [])))
            out = run_check(db, insp, BY_NO[6], frame, CheckInputs(codes=["bar123", "tf-7788"]))
            assert out.status == CheckStatus.PASS

            # 6b: AI verdict == "fail" must NOT count taffeta as confirmed pass
            monkeypatch.setattr(verification, "get_provider",
                                lambda: SimpleNamespace(analyze=lambda t: AIResult("fail", 0.95, "TF-7788 damaged", "torn", ["torn"])))
            out_fail = run_check(db, insp, BY_NO[6], frame, CheckInputs(codes=["BAR 123"]))
            assert out_fail.status != CheckStatus.PASS
    finally:
        engine.dispose()


# 7. Controller revisits goto-interrupted check and clears client_codes between attempts
def test_controller_revisits_goto_interrupted_check_and_clears_client_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    SessionLocal, engine = _isolated_session(tmp_path)
    try:
        with SessionLocal() as db:
            insp, _, _ = _seed_inspection(db, po_number="PO-BUG7", barcode="8901234567890", taffeta="TF-7788-M")
            insp_id = insp.id

        class _Messenger:
            def __init__(self):
                self.sent = []

            async def send(self, msg):
                self.sent.append(msg)

        async def scenario():
            sampler, msgr = FrameSampler(), _Messenger()
            ctl = InspectionController(insp_id, sampler, msgr,
                                       AgentTiming(settle_s=0.02, window_s=0.05, no_video_timeout_s=0.2, input_timeout_s=0.5),
                                       only=[6, 8], session_factory=SessionLocal)
            # Push sharp frames continuously in the background
            stop = asyncio.Event()

            async def feeder():
                while not stop.is_set():
                    rng = np.random.default_rng(1)
                    sampler.push(np.clip(np.array([40, 60, 110]) + rng.normal(0, 25, (240, 320, 3)), 0, 255).astype("uint8"))
                    await asyncio.sleep(0.02)

            task = asyncio.create_task(feeder())
            # Provide an extra/bogus client code on check 6's 1st attempt; we'll re-run check 6 after goto
            ctl.handle_message({"type": "codes", "check_no": 6, "codes": ["STALE-CODE"]})

            async def reactor():
                # Wait for check 6 result (attempt 1 fails due to STALE-CODE), then during check 8 input wait, goto 6
                while not any(m["type"] == "instruction" and m["check_no"] == 8 for m in msgr.sent):
                    await asyncio.sleep(0.01)
                # Verify client_codes for check 6 was cleared after check 6 ran
                assert ctl.client_codes.get(6, []) == []
                ctl.handle_message({"type": "codes", "check_no": 6, "codes": ["8901234567890", "TF-7788-M"]})
                ctl.handle_message({"type": "goto", "check_no": 6})
                # Wait until check 8 is revisited automatically after goto 6 completes
                while sum(1 for m in msgr.sent if m["type"] == "instruction" and m["check_no"] == 8) < 2:
                    await asyncio.sleep(0.01)
                ctl.handle_message({"type": "readings", "check_no": 8, "measured": {"weight_g": 210}})

            r_task = asyncio.create_task(reactor())
            await ctl.run()
            stop.set()
            await task
            await r_task
            return msgr

        msgr = asyncio.run(scenario())
        done = [m for m in msgr.sent if m["type"] == "done"][-1]
        assert 8 not in done["pending_checks"]
        res6 = [m for m in msgr.sent if m["type"] == "result" and m["check_no"] == 6]
        assert res6[-1]["status"] == CheckStatus.PASS   # STALE-CODE did NOT leak into the second attempt
        res8 = [m for m in msgr.sent if m["type"] == "result" and m["check_no"] == 8]
        assert res8[-1]["status"] == CheckStatus.PASS
    finally:
        engine.dispose()


# 8. PDF _images caps image height and computes caption height dynamically
def test_pdf_images_caps_tall_images_and_computes_caption_height(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    tall_buf = io.BytesIO()
    Image.new("RGB", (200, 1200), (30, 80, 140)).save(tall_buf, format="JPEG")
    rel = storage.save_file(tall_buf.getvalue(), "evidence/tall/1")

    long_reason = "Long failure explanation repeated for wrapping test. " * 8
    checks = [SimpleNamespace(check_no=n, status="fail" if n == 1 else "pass", ai_status="pass",
                              confidence=0.9, expected_value="", observed_value="",
                              reason=long_reason if n == 1 else "", reviewer_id=None,
                              reviewer_comment="", corrective_action="",
                              evidence=[SimpleNamespace(file_path=rel, captured_at=datetime.now(timezone.utc),
                                                        extracted={"outcome": {"status": "fail", "reason": long_reason}})] if n <= 4 else [])
              for n in range(1, 21)]
    insp = SimpleNamespace(
        id=99, created_at=datetime.now(timezone.utc), decided_at=None, decided_by_id=None,
        color_name="NAVY BLUE", size="M", status="submitted", decision_note="", exception_recorded=False,
        checks=checks, po=SimpleNamespace(vendor_code="V100", po_number="PO-PDF", asn_number="", article_number="A"),
    )

    placed = []
    orig_image = report._PDF.image

    def spy_image(self, name, x=None, y=None, w=0, h=0, *args, **kwargs):
        placed.append((y, h, self.page_break_trigger))
        return orig_image(self, name, x=x, y=y, w=w, h=h, *args, **kwargs)

    monkeypatch.setattr(report._PDF, "image", spy_image)
    out = report.build_pdf(insp, [insp])
    assert out.startswith(b"%PDF-")
    assert len(placed) == 4
    for y, h, trigger in placed:
        assert 0 < h <= 70.0
        assert y + h <= trigger


# 9. Check 6: an AI "fail" on the labels is flagged for QC, never passed on the barcode alone
def test_barcode_ok_but_ai_flags_labels_goes_to_qc(monkeypatch):
    po = SimpleNamespace(barcode_number="8901234567890", taffeta_number="TF-7788-M", article_number="ART-77",
                         style_number="ST-9")
    insp = SimpleNamespace(po=po, size="M", color_name="NAVY")
    monkeypatch.setattr("app.services.verification.barcode.decode", lambda frame: [])
    monkeypatch.setattr("app.services.verification.get_provider", lambda: SimpleNamespace(
        analyze=lambda t: AIResult("fail", 0.95, "ART-11 TF-7788-M", "taffeta shows another article", [])))
    out = run_check(None, insp, BY_NO[6], textured_jpeg(), CheckInputs(codes=["8901234567890", "TF-7788-M"]))
    assert out.status == CheckStatus.MANUAL_REVIEW and "AI flagged the labels" in out.reason


# 10. Minimum size is measured on the original photo, not the 512 px copy
def test_wide_label_photo_is_not_too_small():
    buf = io.BytesIO()
    Image.effect_noise((1920, 300), 60).convert("RGB").save(buf, "JPEG")
    assert not any("too small" in i for i in imaging.assess_quality(buf.getvalue()).issues)
    small = io.BytesIO()
    Image.effect_noise((300, 60), 60).convert("RGB").save(small, "JPEG")
    assert any("too small" in i for i in imaging.assess_quality(small.getvalue()).issues)
