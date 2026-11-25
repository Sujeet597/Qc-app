"""Reference master: PO data and approved PPS / sealer images (BRD step 1)."""
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_po_for, require
from app.core.database import get_db
from app.models import PurchaseOrder, ReferenceAsset, Role, User
from app.schemas import POCreate, POOut, ReferenceOut
from app.services import storage

router = APIRouter(prefix="/api/pos", tags=["purchase orders"])


@router.get("", response_model=list[POOut])
def list_pos(db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = select(PurchaseOrder).order_by(PurchaseOrder.id.desc())
    if user.role == Role.VENDOR:
        q = q.where(PurchaseOrder.vendor_code == user.vendor_code)
    return db.scalars(q).all()


@router.post("", response_model=POOut, status_code=201)
def create_po(body: POCreate, db: Session = Depends(get_db), _: User = Depends(require(Role.ADMIN))):
    """In production this is replaced by a sync job from the SRM/ERP PO tables."""
    if db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == body.po_number)):
        raise HTTPException(409, "PO already exists")
    po = PurchaseOrder(**body.model_dump())
    db.add(po)
    db.commit()
    return po


@router.get("/{po_id}", response_model=POOut)
def get_po(po_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return get_po_for(db, po_id, user)


@router.get("/{po_id}/references", response_model=list[ReferenceOut])
def list_references(po_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return get_po_for(db, po_id, user).references


@router.post("/{po_id}/references", response_model=ReferenceOut, status_code=201)
def upload_reference(
    po_id: int,
    kind: str = Form(..., description="pps | sealer | main_label | size_label | packing"),
    color_name: str | None = Form(None),
    size: str | None = Form(None),
    note: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.QC, Role.ADMIN)),
):
    # plain `def`: blocking file and DB work runs in FastAPI's thread pool, not on the event loop
    po = get_po_for(db, po_id, user)
    ext = Path(file.filename or "").suffix.lower() or ".jpg"
    path = storage.save_file(file.file.read(), f"references/{po.po_number}", ext)
    ref = ReferenceAsset(po_id=po.id, kind=kind, color_name=color_name, size=size, note=note, file_path=path)
    db.add(ref)
    db.commit()
    return ref
