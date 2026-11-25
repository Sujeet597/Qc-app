"""Check accuracy fixes: exact label matching, no wrong reference, no pointless reading prompts,
attempt history, AI key / timeout config. Each test builds its own data, so test order does not matter."""
from types import SimpleNamespace

import pytest
from conftest import textured_jpeg
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.checklist import BY_NO
from app.core.config import settings
from app.core.database import Base
from app.models import CheckResult, CheckStatus, Inspection, PurchaseOrder, User
from app.services import ai_vision, verification
from app.services.ai_vision import AIResult
from app.services.evidence import record_evidence
from app.services.verification import CheckInputs, _contains, required_inputs, run_check


@pytest.mark.parametrize("expected,observed,ok", [
    ("S", "SIZE L", False),
    ("L", "XL", False),
    ("M", "SIZE: M", True),
    ("XL", "Size XL / 42", True),
    ("NAVY BLUE", "Colour: navy-blue", True),
    ("NAVY BLUE", "NAVYBLUE", True),
    ("NAVY BLUE", "NAVY BLUEISH", False),
    ("4500123", "PO 45001234", False),
    ("4500123", "PO No. 4500123 / ART 99", True),
    ("TF-7788-M", "TAFFETA TF 7788 M", True),
    ("", "anything", False),
])
def test_label_text_matches_whole_words_only(expected, observed, ok):
    assert _contains(expected, observed) is ok


class _Says:
    """Fake vision AI that reads a fixed text and records what it was asked."""

    def __init__(self, text="", verdict="pass", confidence=0.95):
        self.result = AIResult(verdict, confidence, text, "read", [])
        self.tasks = []

    def analyze(self, task):
        self.tasks.append(task)
        return self.result


PO = SimpleNamespace(color_name="NAVY BLUE", mc="MC-40", barcode_number="8901234567890", taffeta_number="TF-7788-M",
                     weight_target_g=210, weight_tolerance_g=10, gsm_target=None, gsm_tolerance=None,
                     size_chart={"S": {"chest_cm": [50, 1.0]}}, po_number="PO-1", article_number="ART-1",
                     packing_norms={}, asn_number=None, order_qty=0, style_number="ST-1")
INSP = SimpleNamespace(po=PO, size="S", color_name="NAVY BLUE")


@pytest.fixture
def auto(monkeypatch):
    """AI decisions switched on, as after the pilot (BRD section 10)."""
    monkeypatch.setattr(settings, "ai_auto_decide", True)


@pytest.mark.parametrize("label,status", [("SIZE L", CheckStatus.FAIL), ("SIZE S", CheckStatus.PASS)])
def test_size_label_s_no_longer_matches_l(monkeypatch, auto, label, status):
    monkeypatch.setattr(verification, "get_provider", lambda: _Says(label))
    monkeypatch.setattr(verification, "_reference", lambda db, insp, kind: b"label")
    assert run_check(None, INSP, BY_NO[5], textured_jpeg(), CheckInputs()).status == status


def test_main_label_and_inner_pack_never_compared_with_sealer(monkeypatch):
    ai = _Says("ok")
    monkeypatch.setattr(verification, "get_provider", lambda: ai)
    monkeypatch.setattr(verification, "_reference", lambda db, insp, kind: b"sealer" if kind == "sealer" else None)
    for no in (4, 15):  # main label, inner pack: only a sealer is uploaded
        out = run_check(None, INSP, BY_NO[no], textured_jpeg(), CheckInputs())
        assert out.status == CheckStatus.MANUAL_REVIEW and BY_NO[no].reference_kind in out.reason
    assert ai.tasks == []
    run_check(None, INSP, BY_NO[7], textured_jpeg(), CheckInputs())  # trims: the sealer is the right reference
    assert ai.tasks[0].reference == b"sealer"


def test_reading_only_asked_when_po_has_a_target():
    assert required_inputs(INSP, 8) == ["weight_g"]      # weight target on PO
    assert required_inputs(INSP, 10) == []               # no GSM target: do not wait for a reading
    assert required_inputs(INSP, 9) == ["chest_cm"]
    assert required_inputs(SimpleNamespace(po=PO, size="XXL"), 9) == []   # size not in size chart
    assert required_inputs(INSP, 17) == ["supplier_code"]
    assert required_inputs(INSP, 1) == []


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _inspection(db) -> Inspection:
    vendor = User(username="v", password_hash="x", role="vendor", vendor_code="V1")
    po = PurchaseOrder(po_number="PO-9", vendor_code="V1", article_number="A", style_number="S", color_name="RED",
                       mc="MC", barcode_number="1", taffeta_number="2", sizes=["M"],
                       weight_target_g=210, weight_tolerance_g=10)
    db.add_all([vendor, po])
    db.flush()
    insp = Inspection(po_id=po.id, vendor_user_id=vendor.id, color_name="RED", size="M",
                      checks=[CheckResult(check_no=8)])
    db.add(insp)
    db.commit()
    return insp


def _weigh(db, insp, grams):
    return record_evidence(db, insp, 8, textured_jpeg(), CheckInputs(measured={"weight_g": grams}))


def test_every_attempt_keeps_its_own_result(db):
    insp = _inspection(db)
    _weigh(db, insp, 300)
    chk = _weigh(db, insp, 212)
    assert chk.status == CheckStatus.PASS
    assert [e.extracted["outcome"]["status"] for e in chk.evidence] == ["fail", "pass"]
    assert chk.evidence[0].extracted["outcome"]["observed"] == "weight_g 300"


def test_recapture_moves_qc_review_into_history_and_cannot_turn_qc_fail_into_pass(db):
    insp = _inspection(db)
    chk = _weigh(db, insp, 300)
    chk.status, chk.reviewer_id, chk.reviewer_comment = CheckStatus.FAIL, 1, "seam open"
    db.commit()

    chk = _weigh(db, insp, 212)
    assert chk.ai_status == CheckStatus.PASS
    assert chk.status == CheckStatus.MANUAL_REVIEW and "QC failed this check earlier" in chk.reason
    assert chk.reviewer_id is None and chk.reviewer_comment == ""
    assert chk.evidence[-1].extracted["replaced_review"]["comment"] == "seam open"


def test_recapture_after_qc_pass_takes_the_new_result(db):
    insp = _inspection(db)
    chk = _weigh(db, insp, 212)
    chk.reviewer_id, chk.reviewer_comment = 1, "ok"
    db.commit()
    chk = _weigh(db, insp, 300)
    assert chk.status == CheckStatus.FAIL and chk.reviewer_id is None
    assert chk.evidence[-1].extracted["replaced_review"]["status"] == "pass"


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "claude")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ai_vision.get_provider.cache_clear()
    yield
    ai_vision.get_provider.cache_clear()


def test_claude_without_key_says_so_instead_of_failing(claude):
    r = ai_vision.get_provider().analyze(None)
    assert r.verdict == "unsure" and "ANTHROPIC_API_KEY is not set" in r.reason


def test_claude_key_and_timeout_come_from_settings(claude, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(settings, "ai_timeout_s", 12.0)
    provider = ai_vision.get_provider()
    assert isinstance(provider, ai_vision.ClaudeProvider)
    assert provider.client.api_key == "sk-test" and provider.client.timeout == 12.0
    assert provider.client.max_retries == settings.ai_max_retries
