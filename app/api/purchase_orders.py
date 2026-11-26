"""Reference master: PO data and approved PPS / sealer images (BRD step 1)."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_po_for, require
from app.core.database import get_db
from app.models import Inspection, PurchaseOrder, ReferenceAsset, Role, User
from app.schemas import POCreate, POOut, POUpdate, ReferenceOut
from app.services import storage, uploads

router = APIRouter(prefix="/api/pos", tags=["purchase orders"])

# Reference kinds the checks look for (checklist.reference_kind). Anything else would be stored but never used.
REFERENCE_KINDS = ("pps", "sealer", "main_label", "size_label", "packing")


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


@router.patch("/{po_id}", response_model=POOut)
def update_po(po_id: int, body: POUpdate, db: Session = Depends(get_db), user: User = Depends(require(Role.ADMIN))):
    """Correct PO data, norms and tolerances. New values apply to checks captured from now on;
    results already recorded keep the expected value they were judged against."""
    po = get_po_for(db, po_id, user)
    changes = body.model_dump(exclude_unset=True)
    inspections = db.scalars(select(Inspection).where(Inspection.po_id == po.id)).all()
    if inspections:
        # these define which inspections exist and who sees them - changing them would orphan past inspections
        for field in ("vendor_code", "color_name"):
            if field in changes and changes[field] != getattr(po, field):
                raise HTTPException(409, f"Cannot change {field}: this PO already has inspections")
        if "sizes" in changes:
            used = {i.size for i in inspections} - set(changes["sizes"] or [])
            if used:
                raise HTTPException(409, f"Cannot remove sizes that have inspections: {sorted(used)}")
    for field, value in changes.items():
        if value is None and field not in ("asn_number", "weight_target_g", "weight_tolerance_g",
                                           "gsm_target", "gsm_tolerance"):
            raise HTTPException(422, f"{field} cannot be empty")
        setattr(po, field, value)
    db.commit()
    return po


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
    if kind not in REFERENCE_KINDS:
        raise HTTPException(422, f"kind must be one of {list(REFERENCE_KINDS)}")
    data, ext = uploads.read_image(file)   # size / type / pixel checks; extension from the content, not the name
    path = storage.save_file(data, f"references/{po.po_number}", ext)
    ref = ReferenceAsset(po_id=po.id, kind=kind, color_name=(color_name or "").strip() or None,
                         size=(size or "").strip() or None, note=note, file_path=path)
    db.add(ref)
    db.commit()
    return ref


def _reference_for(db: Session, po_id: int, ref_id: int, user: User) -> ReferenceAsset:
    po = get_po_for(db, po_id, user)
    ref = db.get(ReferenceAsset, ref_id)
    if not ref or ref.po_id != po.id:
        raise HTTPException(404, "Reference not found")
    return ref


@router.get("/{po_id}/references/{ref_id}/file")
def reference_file(po_id: int, ref_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The approved reference photo, so QC / vendor can see what the checks compare against."""
    ref = _reference_for(db, po_id, ref_id, user)
    try:
        data = storage.read_file(ref.file_path)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "Reference file missing from storage")
    media = "image/png" if ref.file_path.endswith(".png") else "image/jpeg"
    return Response(data, media_type=media, headers={"Cache-Control": "private, max-age=3600"})


@router.delete("/{po_id}/references/{ref_id}", status_code=204)
def delete_reference(po_id: int, ref_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require(Role.QC, Role.ADMIN))):
    """Remove a wrong reference so future checks stop using it. The file is kept on disk for audit:
    results already recorded were judged against it."""
    db.delete(_reference_for(db, po_id, ref_id, user))
    db.commit()
