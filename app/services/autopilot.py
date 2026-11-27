"""Autopilot: final approval when QC is not available (senior-approved safe version).

Rules (BRD section 7: authorized QC decides critical defects and exceptions; section 10: automate only
after validation):
- off unless AUTOPILOT_ENABLED=true
- only a submitted inspection whose 20 checks are ALL Pass (no Fail, no Manual Review, no critical failure)
- only after AUTOPILOT_WAIT_HOURS since submission, and only if QC has not reviewed anything since then
- the decision is marked "Auto-approved by system (autopilot)" and has no approver; QC can reopen it
AI-judged checks only reach Pass on their own when AI_AUTO_DECIDE=true, so with AI as a suggestion
(the current setting) a clean inspection means rule-based passes and passes QC already confirmed.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CheckStatus, Inspection, InspectionStatus

log = logging.getLogger("qc.autopilot")

AUTO_NOTE = "Auto-approved by system (autopilot)"


def is_auto_approved(insp) -> bool:
    return (insp.status == InspectionStatus.PASSED and getattr(insp, "decided_by_id", None) is None
            and (insp.decision_note or "").startswith(AUTO_NOTE))


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)   # SQLite returns naive datetimes


def why_not(insp: Inspection, now: datetime) -> str | None:
    """None if the inspection may be auto-approved now, otherwise the reason it must wait for QC."""
    if insp.status != InspectionStatus.SUBMITTED:
        return "not submitted"
    if any(c.status != CheckStatus.PASS for c in insp.checks):
        return "not all checks passed - QC must decide"
    submitted = _aware(insp.submitted_at)
    if submitted is None or now - submitted < timedelta(hours=settings.autopilot_wait_hours):
        return "waiting time not over"
    if any(c.reviewer_id is not None and _aware(c.updated_at) and _aware(c.updated_at) > submitted
           for c in insp.checks):
        return "QC is working on it"
    return None


def run_once(db: Session, now: datetime | None = None) -> list[int]:
    """Approve every eligible inspection; returns their ids. Safe to run repeatedly."""
    if not settings.autopilot_enabled:
        return []
    now = now or datetime.now(timezone.utc)
    approved = []
    for insp in db.scalars(select(Inspection).where(Inspection.status == InspectionStatus.SUBMITTED)).all():
        if why_not(insp, now) is None:
            insp.status = InspectionStatus.PASSED
            insp.decided_by_id = None
            insp.decided_at = now
            insp.decision_note = (f"{AUTO_NOTE}: all {len(insp.checks)} checks passed, no QC action within "
                                  f"{settings.autopilot_wait_hours:g} h of submission.")
            approved.append(insp.id)
    if approved:
        db.commit()
        log.info("autopilot approved inspections %s", approved)
    return approved
