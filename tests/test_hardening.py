"""Unit tests for input hardening: no shared DB state, so they are independent of test order."""
import io
from datetime import datetime, timezone
from types import SimpleNamespace

import jwt
import pytest
from openpyxl import load_workbook

from app.agent.controller import sender_allowed
from app.api.inspections import _safe_name
from app.checklist import BY_NO
from app.core.config import settings
from app.models import CheckStatus
from app.services import report, video
from app.services.verification import CheckInputs, _measurement

PO = SimpleNamespace(weight_target_g=210, weight_tolerance_g=10, gsm_target=180, gsm_tolerance=8,
                     size_chart={"M": {"chest_cm": [52, 1.0]}})
INSP = SimpleNamespace(po=PO, size="M")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "abc", None, True, [1]])
def test_invalid_reading_goes_to_manual_review_never_pass(value):
    out = _measurement(BY_NO[8], INSP, CheckInputs(measured={"weight_g": value}), {})
    assert out.status == CheckStatus.MANUAL_REVIEW and "Invalid reading" in out.reason


def test_valid_readings_still_decide():
    assert _measurement(BY_NO[8], INSP, CheckInputs(measured={"weight_g": "214"}), {}).status == CheckStatus.PASS
    assert _measurement(BY_NO[10], INSP, CheckInputs(measured={"gsm": 200}), {}).status == CheckStatus.FAIL
    assert _measurement(BY_NO[9], INSP, CheckInputs(measured={"chest_cm": 52.5}), {}).status == CheckStatus.PASS


@pytest.mark.parametrize("kind,identity,ok", [
    ("codes", "vendor-4", False),          # vendor must not inject barcode values (critical check 6)
    ("codes", "qc-2", True),
    ("readings", "vendor-4", True),
    ("skip", "vendor-4", True),
    ("goto", "approver-3", True),
    ("capture", "qc-agent-7", False),      # the agent itself
    ("readings", None, False),             # unknown sender
    ("readings", "vendor-x", False),       # not a server-issued identity
    ("bogus", "qc-2", False),
    (None, "qc-2", False),
])
def test_data_channel_sender_policy(kind, identity, ok):
    assert sender_allowed(kind, identity) is ok


def test_room_token_allows_camera_and_mic_only(monkeypatch):
    monkeypatch.setattr(settings, "livekit_api_key", "key")
    monkeypatch.setattr(settings, "livekit_api_secret", "secret-that-is-long-enough-for-hs256-signing")
    claims = jwt.decode(video.room_token("qc-1", "vendor-4", "V", can_publish=True),
                        settings.livekit_api_secret, algorithms=["HS256"])
    assert claims["video"]["canPublishSources"] == ["camera", "microphone"]
    agent = jwt.decode(video.room_token("qc-1", "qc-agent-1", "A", can_publish=False, hidden=True),
                       settings.livekit_api_secret, algorithms=["HS256"])
    assert agent["video"]["canPublish"] is False and "canPublishSources" not in agent["video"]


def test_report_stores_formula_like_text_as_text():
    evil = '=HYPERLINK("http://example.invalid","click")'
    checks = [SimpleNamespace(check_no=n, status="pass", ai_status="pass", confidence=None, expected_value="",
                              observed_value=evil if n == 2 else "", reason="", reviewer_comment="",
                              corrective_action="", evidence=[]) for n in range(1, 21)]
    insp = SimpleNamespace(id=1, created_at=datetime.now(timezone.utc), decided_at=None, color_name="NAVY",
                           size="M", status="submitted", decision_note=evil, checks=checks,
                           po=SimpleNamespace(vendor_code="V001", po_number="PO-1", asn_number="", article_number="A"))
    ws = load_workbook(io.BytesIO(report.build_excel(insp, [insp])))
    cells = [c for sheet in ws for row in sheet.iter_rows() for c in row if c.value == evil]
    assert len(cells) == 3 and all(c.data_type == "s" for c in cells)  # observed, decision note, history note


def test_safe_download_name():
    assert _safe_name('PO-1"\r\nX: y/../z') == "PO-1___X__y_.._z"
