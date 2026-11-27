"""Reports & dashboard: PO-wise, vendor-wise, article-wise, checklist-wise (BRD section 6)."""
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.checklist import BY_NO
from app.core.database import get_db
from app.services import autopilot
from app.models import CheckStatus, Inspection, Role, User

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary")
def summary(db: Session = Depends(get_db), _: User = Depends(require(Role.QC, Role.APPROVER, Role.ADMIN))):
    inspections = db.scalars(select(Inspection)).all()
    by_status = Counter(i.status for i in inspections)
    by_vendor: dict[str, Counter] = defaultdict(Counter)
    by_po: dict[str, Counter] = defaultdict(Counter)
    by_article: dict[str, Counter] = defaultdict(Counter)
    by_check: dict[int, Counter] = defaultdict(Counter)
    fails_by_check: Counter = Counter()
    for i in inspections:
        by_vendor[i.po.vendor_code][i.status] += 1
        by_po[i.po.po_number][i.status] += 1
        by_article[i.po.article_number][i.status] += 1
        for c in i.checks:
            by_check[c.check_no][c.status] += 1
            if c.status == CheckStatus.FAIL:
                fails_by_check[c.check_no] += 1
    return {
        "total_inspections": len(inspections),
        "auto_approved": sum(autopilot.is_auto_approved(i) for i in inspections),   # passed by autopilot, not a person
        "by_status": by_status,
        "by_vendor": by_vendor,
        "by_po": by_po,
        "by_article": by_article,
        "by_check": [{"check_no": n, "title": BY_NO[n].title, **by_check[n]} for n in sorted(by_check)],
        "top_failing_checks": [{"check_no": n, "title": BY_NO[n].title, "fails": k}
                               for n, k in fails_by_check.most_common(10)],
    }
