"""Database tables. Mirrors BRD section 6 (modules) and section 8 (report fields)."""
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(StrEnum):
    VENDOR = "vendor"
    QC = "qc"
    APPROVER = "approver"
    ADMIN = "admin"


class CheckStatus(StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    MANUAL_REVIEW = "manual_review"


class InspectionStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"          # waiting for QC / approver
    PASSED = "passed"
    FAILED = "failed"
    REINSPECTION = "reinspection_required"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))
    vendor_code: Mapped[str | None] = mapped_column(String(32), nullable=True)  # set for vendor users
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class NominatedVendor(Base):
    """Approved suppliers for carton / pocketing / zip / poly bag (checks 17-20)."""
    __tablename__ = "nominated_vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(32), index=True)  # carton | pocketing | zip | polybag
    supplier_code: Mapped[str] = mapped_column(String(64))
    supplier_name: Mapped[str] = mapped_column(String(128))


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    asn_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vendor_code: Mapped[str] = mapped_column(String(32), index=True)
    article_number: Mapped[str] = mapped_column(String(64))
    style_number: Mapped[str] = mapped_column(String(64))
    color_name: Mapped[str] = mapped_column(String(64))
    mc: Mapped[str] = mapped_column(String(64))                       # master carton spec as on PO
    barcode_number: Mapped[str] = mapped_column(String(64))
    taffeta_number: Mapped[str] = mapped_column(String(64))
    sizes: Mapped[list] = mapped_column(JSON, default=list)           # ["S","M","L"]
    weight_target_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_tolerance_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    gsm_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    gsm_tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    # {"M": {"chest_cm": [52, 1.0], "length_cm": [70, 1.5]}}  -> [target, +/- tolerance]
    size_chart: Mapped[dict] = mapped_column(JSON, default=dict)
    packing_norms: Mapped[dict] = mapped_column(JSON, default=dict)  # {"pcs_per_inner": 5, ...}
    order_qty: Mapped[int] = mapped_column(Integer, default=0)

    references: Mapped[list["ReferenceAsset"]] = relationship(back_populates="po", cascade="all, delete-orphan")
    suppliers: Mapped[list["PoSupplier"]] = relationship(cascade="all, delete-orphan")
    shipments: Mapped[list["AsnShipment"]] = relationship(cascade="all, delete-orphan")


class PoSupplier(Base):
    """Supplier code + batch recorded for a PO per category (checks 17-20). Senior's decision: these come
    from the database, not from vendor typing. Local table until the SRM / Supabase table is confirmed."""
    __tablename__ = "po_suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))          # carton | pocketing | zip | polybag
    supplier_code: Mapped[str] = mapped_column(String(64))
    batch_no: Mapped[str] = mapped_column(String(64), default="")


class AsnShipment(Base):
    """Shipped quantity per ASN (check 14 compares the packing list with it). Local table until SRM data is linked."""
    __tablename__ = "asn_shipments"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    asn_number: Mapped[str] = mapped_column(String(64))
    shipped_qty: Mapped[int] = mapped_column(Integer)


class ReferenceAsset(Base):
    """Approved PPS / sealer images, label artwork etc. stored against the PO."""
    __tablename__ = "reference_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))   # pps | sealer | main_label | size_label | print | packing ...
    color_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[str | None] = mapped_column(String(16), nullable=True)
    file_path: Mapped[str] = mapped_column(String(512))
    note: Mapped[str] = mapped_column(Text, default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    po: Mapped[PurchaseOrder] = relationship(back_populates="references")


class Inspection(Base):
    __tablename__ = "inspections"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    vendor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    color_name: Mapped[str] = mapped_column(String(64))
    size: Mapped[str] = mapped_column(String(16))
    inspection_type: Mapped[str] = mapped_column(String(32), default="bulk_final")
    status: Mapped[str] = mapped_column(String(32), default=InspectionStatus.IN_PROGRESS)
    parent_inspection_id: Mapped[int | None] = mapped_column(ForeignKey("inspections.id"), nullable=True)
    video_room: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    exception_recorded: Mapped[bool] = mapped_column(Boolean, default=False)

    po: Mapped[PurchaseOrder] = relationship()
    checks: Mapped[list["CheckResult"]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan", order_by="CheckResult.check_no"
    )


class CheckResult(Base):
    __tablename__ = "check_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspections.id"), index=True)
    check_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=CheckStatus.PENDING)
    ai_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # what the system decided before review
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_value: Mapped[str] = mapped_column(Text, default="")
    observed_value: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewer_comment: Mapped[str] = mapped_column(Text, default="")
    corrective_action: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    inspection: Mapped[Inspection] = relationship(back_populates="checks")
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="check", cascade="all, delete-orphan")


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    check_result_id: Mapped[int] = mapped_column(ForeignKey("check_results.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)  # barcode values, OCR text, delta-E, AI raw result

    check: Mapped[CheckResult] = relationship(back_populates="evidence")
