"""User management (admin only): add vendors / QC staff, change roles, disable accounts, reset passwords.
Users are never deleted, only disabled, so inspections, reviews and decisions keep their author."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.database import get_db
from app.core.security import hash_password
from app.models import Role, User
from app.schemas import PasswordReset, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/users", tags=["users"])
admin_only = require(Role.ADMIN)


def _vendor_code(role: str, vendor_code: str | None) -> str | None:
    """A vendor must have a vendor code (it decides which POs they see); staff must not have one."""
    code = (vendor_code or "").strip() or None
    if role == Role.VENDOR and not code:
        raise HTTPException(422, "A vendor user needs a vendor_code")
    if role != Role.VENDOR and code:
        raise HTTPException(422, "Only vendor users have a vendor_code")
    return code


def _get(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return user


@router.get("", response_model=list[UserOut])
def list_users(role: str | None = None, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    q = select(User).order_by(User.id)
    if role:
        q = q.where(User.role == role)
    return db.scalars(q).all()


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "Username already exists")
    user = User(username=body.username, full_name=body.full_name, role=body.role,
                vendor_code=_vendor_code(body.role, body.vendor_code), password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdate, db: Session = Depends(get_db), admin: User = Depends(admin_only)):
    user = _get(db, user_id)
    changes = body.model_dump(exclude_unset=True)
    # an admin cannot lock themselves out (last way back in would be the database)
    if user.id == admin.id and (changes.get("active") is False or changes.get("role", Role.ADMIN) != Role.ADMIN):
        raise HTTPException(409, "You cannot disable yourself or remove your own admin role")
    role = changes.get("role", user.role)
    if "role" in changes or "vendor_code" in changes:
        user.vendor_code = _vendor_code(role, changes.get("vendor_code", user.vendor_code if role == Role.VENDOR else None))
    for field in ("full_name", "role", "active"):
        if field in changes and changes[field] is not None:
            setattr(user, field, changes[field])
    db.commit()
    return user


@router.post("/{user_id}/password", status_code=204)
def reset_password(user_id: int, body: PasswordReset, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    user = _get(db, user_id)
    user.password_hash = hash_password(body.new_password)
    db.commit()
