"""Save one captured frame as evidence and run the checklist verification on it.
Shared by the REST endpoint (browser capture) and the live video agent.

Every capture is kept as an attempt: its Evidence row stores the result that capture produced
("outcome"), so a re-capture never erases what an earlier attempt showed. The CheckResult row
holds the latest result. If QC had already reviewed the check, that review is moved into the new
attempt's record ("replaced_review") and cleared, so the check shows the new result, not an old
reviewer's name next to it. A check QC failed cannot turn into a Pass by re-capturing: it goes to
Manual Review for QC to confirm.
"""
from sqlalchemy.orm import Session

from app.checklist import BY_NO
from app.models import CheckResult, CheckStatus, Evidence, Inspection, InspectionStatus
from app.services import storage
from app.services.verification import CheckInputs, Outcome, run_check


class EvidenceError(ValueError):
    pass


def _take_review(chk: CheckResult) -> dict | None:
    """Remove QC's review from the check and return it for the attempt history."""
    if chk.reviewer_id is None:
        return None
    review = {"status": chk.status, "reviewer_id": chk.reviewer_id, "comment": chk.reviewer_comment,
              "corrective_action": chk.corrective_action, "ai_status": chk.ai_status}
    chk.reviewer_id = None
    chk.reviewer_comment = chk.corrective_action = ""
    return review


def _prior_qc_failed(chk: CheckResult, taken_review: dict | None) -> bool:
    """True if QC's most recent review on this check was Fail (persists across multiple re-captures)."""
    if taken_review is not None:
        return taken_review["status"] == CheckStatus.FAIL
    latest = max((e for e in chk.evidence if (e.extracted or {}).get("replaced_review")),
                 key=lambda e: e.id or 0, default=None)
    return bool(latest and latest.extracted["replaced_review"]["status"] == CheckStatus.FAIL)


def record_evidence(db: Session, insp: Inspection, check_no: int, frame: bytes, inputs: CheckInputs,
                    source: str = "upload") -> CheckResult:
    if insp.status != InspectionStatus.IN_PROGRESS:
        raise EvidenceError("Inspection is not in progress")
    chk = next((c for c in insp.checks if c.check_no == check_no), None)
    if chk is None or check_no not in BY_NO:
        raise EvidenceError("Checklist point not found")
    if not frame:
        raise EvidenceError("Empty frame")

    path = storage.save_file(frame, f"evidence/{insp.id}/{check_no}")
    try:
        outcome = run_check(db, insp, BY_NO[check_no], frame, inputs)
        extracted = dict(outcome.extracted)
    except Exception as e:  # a bad image or AI failure must never silently pass
        outcome = Outcome(CheckStatus.MANUAL_REVIEW, None, chk.expected_value, "",
                          f"Automatic check failed ({type(e).__name__}) - QC to verify.")
        extracted = {"error": str(e)}

    db.refresh(chk)
    review = _take_review(chk)
    status = outcome.status
    reason = outcome.reason
    if _prior_qc_failed(chk, review) and status == CheckStatus.PASS:
        status = CheckStatus.MANUAL_REVIEW
        reason = f"QC failed this check earlier - QC to confirm the new capture. System: {outcome.reason}"

    chk.ai_status = outcome.status
    chk.status = status
    chk.confidence = outcome.confidence
    chk.expected_value = outcome.expected
    chk.observed_value = outcome.observed
    chk.reason = reason
    extracted.update(source=source, outcome={
        "status": status, "ai_status": outcome.status, "confidence": outcome.confidence,
        "expected": outcome.expected, "observed": outcome.observed, "reason": reason})
    if review:
        extracted["replaced_review"] = review
    chk.evidence.append(Evidence(file_path=path, extracted=extracted))
    db.commit()
    return chk
