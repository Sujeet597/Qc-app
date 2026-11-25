from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models import Inspection, PurchaseOrder, Role, User

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def current_user(token: str | None = Depends(oauth2), x_role: str = Header("vendor"),
                 db: Session = Depends(get_db)) -> User:
    if not settings.auth_enabled:
        # No login: act as the first active demo user with the requested role
        if x_role not in {r.value for r in Role}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"X-Role must be one of {[r.value for r in Role]}")
        user = db.scalar(select(User).where(User.role == x_role, User.active).order_by(User.id))
        if not user:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                f"No '{x_role}' user exists - set SEED_DEMO_DATA=true or create one")
        return user
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    try:
        user_id = int(decode_access_token(token)["sub"])
    except (PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.get(User, user_id)
    if not user or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User inactive")
    return user


def require(*roles: Role):
    def checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed for your role")
        return user
    return checker


def get_po_for(db: Session, po_id: int, user: User) -> PurchaseOrder:
    po = db.get(PurchaseOrder, po_id)
    if not po or (user.role == Role.VENDOR and po.vendor_code != user.vendor_code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PO not found")
    return po


def get_inspection_for(db: Session, insp_id: int, user: User) -> Inspection:
    insp = db.get(Inspection, insp_id)
    if not insp or (user.role == Role.VENDOR and insp.po.vendor_code != user.vendor_code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Inspection not found")
    return insp
