"""Video inspection flow - BRD steps 2 to 5."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_inspection_for, get_po_for, require
from app.checklist import BY_NO, CHECKLIST, input_fields
from app.core.config import settings
from app.core.database import get_db
from app.models import CheckResult, CheckStatus, Inspection, InspectionStatus, Role, User
from app.schemas import CheckOut, DecisionIn, InspectionCreate, InspectionOut, ReviewIn, VideoTokenOut
from app.services import report, video
from app.services.decision import summarize
from app.services.evidence import EvidenceError, record_evidence
from app.services.verification import CheckInputs

router = APIRouter(prefix="/api/inspections", tags=["inspections"])


def _check_out(c: CheckResult, insp: Inspection) -> CheckOut:
    d = BY_NO[c.check_no]
    chart_keys = list((insp.po.size_chart.get(insp.size) or {}).keys())
    return CheckOut.model_validate(c).model_copy(update={
        "title": d.title, "instruction": d.instruction, "critical": d.critical, "method": d.method,
        "input_fields": input_fields(c.check_no, chart_keys), "evidence_count": len(c.evidence)})


def _out(insp: Inspection) -> InspectionOut:
    s = summarize(insp)
    po = insp.po
    return InspectionOut.model_validate(insp).model_copy(update={
        "po_number": po.po_number, "asn_number": po.asn_number, "article_number": po.article_number,
        "vendor_code": po.vendor_code, "progress": s["progress"], "summary": s["state"],
        "checks": [_check_out(c, insp) for c in insp.checks]})


def _check(insp: Inspection, no: int) -> CheckResult:
    chk = next((c for c in insp.checks if c.check_no == no), None)
    if not chk:
        raise HTTPException(404, "Checklist point not found")
    return chk


@router.post("", response_model=InspectionOut, status_code=201)
def start_inspection(body: InspectionCreate, db: Session = Depends(get_db), user: User = Depends(require(Role.VENDOR))):
    po = get_po_for(db, body.po_id, user)
    if body.size not in po.sizes:
        raise HTTPException(422, f"Size must be one of {po.sizes}")
    previous = db.scalar(select(Inspection).where(Inspection.po_id == po.id, Inspection.color_name == body.color_name,
                                                  Inspection.size == body.size).order_by(Inspection.id.desc()))
    if previous and previous.status in (InspectionStatus.IN_PROGRESS, InspectionStatus.SUBMITTED):
        raise HTTPException(409, f"Inspection {previous.id} is still open for this PO/color/size")
    insp = Inspection(po_id=po.id, vendor_user_id=user.id, color_name=body.color_name, size=body.size,
                      inspection_type=body.inspection_type,
                      parent_inspection_id=previous.id if previous and previous.status == InspectionStatus.REINSPECTION else None,
                      checks=[CheckResult(check_no=c.no) for c in CHECKLIST])
    db.add(insp)
    db.commit()
    insp.video_room = f"qc-{insp.id}"
    db.commit()
    return _out(insp)


@router.get("", response_model=list[InspectionOut])
def list_inspections(status: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = select(Inspection).order_by(Inspection.id.desc())
    if user.role == Role.VENDOR:
        q = q.where(Inspection.vendor_user_id == user.id)
    if status:
        q = q.where(Inspection.status == status)
    return [_out(i) for i in db.scalars(q).all()]


@router.get("/{insp_id}", response_model=InspectionOut)
def get_inspection(insp_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _out(get_inspection_for(db, insp_id, user))


@router.post("/{insp_id}/video-token", response_model=VideoTokenOut)
def video_token(insp_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Vendor publishes camera; QC / approver join to watch and guide."""
    insp = get_inspection_for(db, insp_id, user)
    if not video.livekit_enabled():
        raise HTTPException(503, "Video not configured - set LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET")
    # everyone may publish (QC talks to the vendor); the agent only watches identities starting with "vendor-"
    token = video.room_token(insp.video_room, f"{user.role}-{user.id}", user.full_name or user.username,
                             can_publish=True)
    return VideoTokenOut(url=settings.livekit_url, room=insp.video_room, token=token)


_agents: dict[int, subprocess.Popen] = {}
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@router.post("/{insp_id}/agent", status_code=202)
def start_agent(insp_id: int, db: Session = Depends(get_db), user: User = Depends(require(Role.VENDOR, Role.QC))):
    """Start the live QC agent: it joins the video call, guides the vendor and verifies frames itself.
    Dev setup runs it as a subprocess; in production run app.agent.livekit_worker as its own service."""
    insp = get_inspection_for(db, insp_id, user)
    if insp.status != InspectionStatus.IN_PROGRESS:
        raise HTTPException(409, "Inspection is not in progress")
    if not video.livekit_enabled():
        raise HTTPException(503, "Video not configured - set LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET")
    proc = _agents.get(insp_id)
    if proc and proc.poll() is None:
        return {"status": "already_running", "pid": proc.pid}
    proc = subprocess.Popen([sys.executable, "-m", "app.agent.livekit_worker", "--inspection", str(insp_id)],
                            cwd=_PROJECT_ROOT)
    _agents[insp_id] = proc
    return {"status": "started", "pid": proc.pid, "room": insp.video_room}


@router.get("/{insp_id}/agent")
def agent_status(insp_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    get_inspection_for(db, insp_id, user)
    proc = _agents.get(insp_id)
    if not proc:
        return {"status": "not_started"}
    code = proc.poll()
    return {"status": "running" if code is None else "finished", "exit_code": code}


@router.post("/{insp_id}/checks/{check_no}/evidence", response_model=CheckOut)
async def submit_evidence(
    insp_id: int,
    check_no: int,
    frame: UploadFile = File(..., description="JPEG/PNG frame captured from the live video"),
    codes: str = Form("", description="Comma-separated barcodes decoded in the browser"),
    measured: str = Form("", description='JSON readings, e.g. {"weight_g": 212}'),
    supplier_code: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.VENDOR, Role.QC)),
):
    """Capture evidence for one checklist point and run AI / rule verification on it (BRD step 3-4)."""
    insp = get_inspection_for(db, insp_id, user)
    try:
        readings = json.loads(measured) if measured else {}
        if not isinstance(readings, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(422, "measured must be a JSON object")
    inputs = CheckInputs(codes=[c.strip() for c in codes.split(",") if c.strip()], measured=readings,
                         supplier_code=supplier_code.strip() or None)
    try:
        chk = record_evidence(db, insp, check_no, await frame.read(), inputs, source="upload")
    except EvidenceError as e:
        raise HTTPException(409 if "progress" in str(e) else 422, str(e))
    return _check_out(chk, insp)


@router.post("/{insp_id}/submit", response_model=InspectionOut)
def submit(insp_id: int, db: Session = Depends(get_db), user: User = Depends(require(Role.VENDOR))):
    insp = get_inspection_for(db, insp_id, user)
    if insp.status != InspectionStatus.IN_PROGRESS:
        raise HTTPException(409, "Already submitted")
    pending = [c.check_no for c in insp.checks if c.status == CheckStatus.PENDING]
    if pending:
        raise HTTPException(422, f"Checks not done yet: {pending}")
    insp.status = InspectionStatus.SUBMITTED
    insp.submitted_at = datetime.now(timezone.utc)
    db.commit()
    return _out(insp)


@router.post("/{insp_id}/checks/{check_no}/review", response_model=InspectionOut)
def review_check(insp_id: int, check_no: int, body: ReviewIn, db: Session = Depends(get_db),
                 user: User = Depends(require(Role.QC, Role.APPROVER))):
    """QC team resolves Manual Review / overrides an AI result. AI result is kept in ai_status for audit."""
    insp = get_inspection_for(db, insp_id, user)
    if insp.status not in (InspectionStatus.IN_PROGRESS, InspectionStatus.SUBMITTED):
        raise HTTPException(409, "Inspection already decided")
    chk = _check(insp, check_no)
    if body.status == "pass" and not chk.evidence:
        raise HTTPException(422, "Cannot pass a check without evidence")
    chk.status = body.status
    chk.reviewer_id = user.id
    chk.reviewer_comment = body.comment
    chk.corrective_action = body.corrective_action
    db.commit()
    return _out(insp)


@router.post("/{insp_id}/decision", response_model=InspectionOut)
def final_decision(insp_id: int, body: DecisionIn, db: Session = Depends(get_db),
                   user: User = Depends(require(Role.APPROVER))):
    """BRD step 5 / section 7: final Pass / Fail / Reinspection by QC authority."""
    insp = get_inspection_for(db, insp_id, user)
    if insp.status != InspectionStatus.SUBMITTED:
        raise HTTPException(409, "Inspection must be submitted first")
    s = summarize(insp)
    if body.decision == "passed":
        if s["open_reviews"]:
            raise HTTPException(422, f"Resolve manual reviews first: {s['open_reviews']}")
        if s["critical_failures"] and not body.record_exception:
            raise HTTPException(422, f"Critical failures {s['critical_failures']} block approval "
                                     "unless an authorized exception is recorded")
        if s["critical_failures"] and not body.note.strip():
            raise HTTPException(422, "Exception requires a note")
        insp.exception_recorded = bool(s["critical_failures"])
    insp.status = body.decision
    insp.decision_note = body.note
    insp.decided_by_id = user.id
    insp.decided_at = datetime.now(timezone.utc)
    db.commit()
    return _out(insp)


@router.get("/{insp_id}/report.xlsx")
def download_report(insp_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    insp = get_inspection_for(db, insp_id, user)
    history = db.scalars(select(Inspection).where(Inspection.po_id == insp.po_id, Inspection.color_name == insp.color_name,
                                                  Inspection.size == insp.size).order_by(Inspection.id)).all()
    content = report.build_excel(insp, list(history))
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="QC_{insp.po.po_number}_{insp.id}.xlsx"'})
