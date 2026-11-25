"""Demo users and one demo PO so the API can be tried from /docs. Disable with SEED_DEMO_DATA=false."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import NominatedVendor, PurchaseOrder, Role, User

DEMO_USERS = [
    ("admin", "admin123", Role.ADMIN, None),
    ("qc", "qc123", Role.QC, None),
    ("approver", "approver123", Role.APPROVER, None),
    ("vendor1", "vendor123", Role.VENDOR, "V001"),
]


def seed(db: Session) -> None:
    if db.scalar(select(User).limit(1)):
        return
    for username, pw, role, vendor in DEMO_USERS:
        db.add(User(username=username, full_name=username.title(), password_hash=hash_password(pw),
                    role=role, vendor_code=vendor))
    db.add(PurchaseOrder(
        po_number="PO-1001", asn_number="ASN-5001", vendor_code="V001",
        article_number="ART-7788", style_number="ST-7788", color_name="NAVY BLUE", mc="20 PCS / CTN",
        barcode_number="8901234567890", taffeta_number="TF-7788-M",
        sizes=["S", "M", "L"], weight_target_g=210, weight_tolerance_g=10, gsm_target=180, gsm_tolerance=8,
        size_chart={"M": {"chest_cm": [52, 1.0], "length_cm": [70, 1.5]}},
        packing_norms={"pcs_per_inner": 5, "inners_per_carton": 4}, order_qty=1200,
    ))
    for cat, code, name in [("carton", "CTN-01", "ABC Cartons"), ("pocketing", "PKT-01", "XYZ Fabrics"),
                            ("zip", "YKK", "YKK"), ("polybag", "PB-01", "PolyPack")]:
        db.add(NominatedVendor(category=cat, supplier_code=code, supplier_name=name))
    db.commit()
