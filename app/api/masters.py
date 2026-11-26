"""Nominated vendor master (BRD section 6 "Reference master"): approved carton / pocketing / zip / poly bag
suppliers that checks 17-20 compare against. Staff can read it; only admin can change it."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.database import get_db
from app.models import NominatedVendor, Role, User
from app.schemas import NominatedVendorIn, NominatedVendorOut, NominatedVendorUpdate

router = APIRouter(prefix="/api/nominated-vendors", tags=["reference master"])
staff = require(Role.QC, Role.APPROVER, Role.ADMIN)
admin_only = require(Role.ADMIN)


def _norm(code: str) -> str:
    return "".join(code.split()).casefold()


def _duplicate(db: Session, category: str, code: str, exclude_id: int | None = None) -> bool:
    """Same supplier code twice in one category would make the check ambiguous (codes compare ignoring case / spaces)."""
    rows = db.scalars(select(NominatedVendor).where(NominatedVendor.category == category)).all()
    return any(_norm(r.supplier_code) == _norm(code) and r.id != exclude_id for r in rows)


def _get(db: Session, vendor_id: int) -> NominatedVendor:
    row = db.get(NominatedVendor, vendor_id)
    if not row:
        raise HTTPException(404, "Nominated vendor not found")
    return row


@router.get("", response_model=list[NominatedVendorOut])
def list_vendors(category: str | None = None, db: Session = Depends(get_db), _: User = Depends(staff)):
    q = select(NominatedVendor).order_by(NominatedVendor.category, NominatedVendor.supplier_code)
    if category:
        q = q.where(NominatedVendor.category == category)
    return db.scalars(q).all()


@router.post("", response_model=NominatedVendorOut, status_code=201)
def add_vendor(body: NominatedVendorIn, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    if _duplicate(db, body.category, body.supplier_code):
        raise HTTPException(409, f"Supplier code already nominated for {body.category}")
    row = NominatedVendor(category=body.category, supplier_code=body.supplier_code.strip(),
                          supplier_name=body.supplier_name.strip())
    db.add(row)
    db.commit()
    return row


@router.patch("/{vendor_id}", response_model=NominatedVendorOut)
def update_vendor(vendor_id: int, body: NominatedVendorUpdate, db: Session = Depends(get_db),
                  _: User = Depends(admin_only)):
    row = _get(db, vendor_id)
    if body.supplier_code is not None:
        if _duplicate(db, row.category, body.supplier_code, exclude_id=row.id):
            raise HTTPException(409, f"Supplier code already nominated for {row.category}")
        row.supplier_code = body.supplier_code.strip()
    if body.supplier_name is not None:
        row.supplier_name = body.supplier_name.strip()
    db.commit()
    return row


@router.delete("/{vendor_id}", status_code=204)
def remove_vendor(vendor_id: int, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    """Past inspections keep their recorded result; only future checks stop accepting this supplier."""
    db.delete(_get(db, vendor_id))
    db.commit()
