"""Save one captured frame as evidence and run the checklist verification on it.
Shared by the REST endpoint (browser capture) and the live video agent."""
from sqlalchemy.orm import Session

from app.checklist import BY_NO
from app.models import CheckResult, CheckStatus, Evidence, Inspection, InspectionStatus
from app.services import storage
from app.services.verification import CheckInputs, run_check


class EvidenceError(ValueError):
    pass


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
    except Exception as e:  # a bad image or AI failure must never silently pass
        chk.status = chk.ai_status = CheckStatus.MANUAL_REVIEW
        chk.confidence = None
        chk.reason = f"Automatic check failed ({type(e).__name__}) - QC to verify."
        chk.evidence.append(Evidence(file_path=path, extracted={"error": str(e), "source": source}))
    else:
        chk.status = chk.ai_status = outcome.status
        chk.confidence = outcome.confidence
        chk.expected_value = outcome.expected
        chk.observed_value = outcome.observed
        chk.reason = outcome.reason
        chk.evidence.append(Evidence(file_path=path, extracted={**outcome.extracted, "source": source}))
    db.commit()
    return chk
