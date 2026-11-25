"""QC decision rules - BRD section 7."""
from app.checklist import BY_NO
from app.core.config import settings
from app.models import CheckStatus, Inspection


def from_ai(verdict: str, confidence: float) -> CheckStatus:
    """Turn an AI verdict into a check status. Low confidence or 'unsure' -> Manual Review, never Pass."""
    if verdict == "unsure" or confidence < settings.ai_confidence_threshold:
        return CheckStatus.MANUAL_REVIEW
    return CheckStatus.PASS if verdict == "pass" else CheckStatus.FAIL


def summarize(insp: Inspection) -> dict:
    statuses = [c.status for c in insp.checks]
    critical_fails = [c.check_no for c in insp.checks if c.status == CheckStatus.FAIL and BY_NO[c.check_no].critical]
    done = sum(s != CheckStatus.PENDING for s in statuses)
    if done < len(statuses):
        state = "incomplete"
    elif critical_fails:
        state = "critical_failure"        # blocks approval unless an exception is recorded
    elif CheckStatus.FAIL in statuses or CheckStatus.MANUAL_REVIEW in statuses:
        state = "needs_qc_review"
    else:
        state = "all_passed"
    return {
        "progress": f"{done} of {len(statuses)} checks completed",
        "state": state,
        "critical_failures": critical_fails,
        "open_reviews": [c.check_no for c in insp.checks if c.status == CheckStatus.MANUAL_REVIEW],
    }
