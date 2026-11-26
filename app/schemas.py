import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class POOut(ORM):
    id: int
    po_number: str
    asn_number: str | None
    vendor_code: str
    article_number: str
    style_number: str
    color_name: str
    mc: str
    barcode_number: str
    taffeta_number: str
    sizes: list[str]
    weight_target_g: float | None
    weight_tolerance_g: float | None
    gsm_target: float | None
    gsm_tolerance: float | None
    size_chart: dict
    packing_norms: dict
    order_qty: int


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_size_chart(chart: object) -> object:
    if chart is None:
        return chart
    if not isinstance(chart, dict):
        raise ValueError("size_chart must be a JSON object")
    for size, points in chart.items():
        if not isinstance(size, str) or not isinstance(points, dict):
            raise ValueError("size_chart must map size to {point: [target, tolerance]}")
        for point, pair in points.items():
            if not isinstance(point, str) or not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(f"size_chart[{size!r}][{point!r}] must be [target, tolerance]")
            t, tol = pair
            if not (_number(t) and _number(tol)):
                raise ValueError(f"size_chart[{size!r}][{point!r}] values must be numbers")
            if tol < 0:
                raise ValueError(f"size_chart[{size!r}][{point!r}] must be finite numbers with tolerance >= 0")
    return chart


def _check_packing_norms(norms: object) -> object:
    """{"pcs_per_inner": 5, "inners_per_carton": 4} - every value a number >= 0."""
    if norms is None:
        return norms
    if not isinstance(norms, dict):
        raise ValueError("packing_norms must be a JSON object")
    for key, value in norms.items():
        if not isinstance(key, str) or not _number(value) or value < 0:
            raise ValueError(f"packing_norms[{key!r}] must be a number >= 0")
    return norms


class POCreate(BaseModel):
    po_number: str = Field(min_length=1, max_length=64)
    asn_number: str | None = None
    vendor_code: str = Field(min_length=1, max_length=32)
    article_number: str
    style_number: str
    color_name: str = Field(min_length=1, max_length=64)
    mc: str
    barcode_number: str
    taffeta_number: str
    sizes: list[str] = Field(min_length=1)
    weight_target_g: float | None = Field(None, ge=0)
    weight_tolerance_g: float | None = Field(None, ge=0)
    gsm_target: float | None = Field(None, ge=0)
    gsm_tolerance: float | None = Field(None, ge=0)
    size_chart: dict[str, dict[str, tuple[float, float]]] = {}
    packing_norms: dict[str, float] = {}
    order_qty: int = Field(0, ge=0)

    _size_chart = field_validator("size_chart", mode="before")(_check_size_chart)
    _packing = field_validator("packing_norms", mode="before")(_check_packing_norms)


class POUpdate(BaseModel):
    """Fields an admin may correct on a PO. Only the fields sent are changed; po_number never changes."""
    model_config = ConfigDict(extra="forbid")
    asn_number: str | None = None
    vendor_code: str | None = Field(None, min_length=1, max_length=32)
    article_number: str | None = None
    style_number: str | None = None
    color_name: str | None = Field(None, min_length=1, max_length=64)
    mc: str | None = None
    barcode_number: str | None = None
    taffeta_number: str | None = None
    sizes: list[str] | None = Field(None, min_length=1)
    weight_target_g: float | None = Field(None, ge=0)
    weight_tolerance_g: float | None = Field(None, ge=0)
    gsm_target: float | None = Field(None, ge=0)
    gsm_tolerance: float | None = Field(None, ge=0)
    size_chart: dict[str, dict[str, tuple[float, float]]] | None = None
    packing_norms: dict[str, float] | None = None
    order_qty: int | None = Field(None, ge=0)

    _size_chart = field_validator("size_chart", mode="before")(_check_size_chart)
    _packing = field_validator("packing_norms", mode="before")(_check_packing_norms)


VENDOR_CATEGORIES = ("carton", "pocketing", "zip", "polybag")


class NominatedVendorIn(BaseModel):
    category: Literal["carton", "pocketing", "zip", "polybag"]
    supplier_code: str = Field(min_length=1, max_length=64)
    supplier_name: str = Field(min_length=1, max_length=128)


class NominatedVendorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_code: str | None = Field(None, min_length=1, max_length=64)
    supplier_name: str | None = Field(None, min_length=1, max_length=128)


class NominatedVendorOut(ORM):
    id: int
    category: str
    supplier_code: str
    supplier_name: str


RoleName = Literal["vendor", "qc", "approver", "admin"]


class UserOut(ORM):
    id: int
    username: str
    full_name: str
    role: str
    vendor_code: str | None
    active: bool


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._@-]+$")
    full_name: str = Field("", max_length=128)
    password: str = Field(min_length=8, max_length=128)
    role: RoleName
    vendor_code: str | None = Field(None, max_length=32)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = Field(None, max_length=128)
    role: RoleName | None = None
    vendor_code: str | None = Field(None, max_length=32)
    active: bool | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class ReferenceOut(ORM):
    id: int
    kind: str
    color_name: str | None
    size: str | None
    note: str
    uploaded_at: datetime


class InspectionCreate(BaseModel):
    po_id: int
    color_name: str
    size: str
    inspection_type: str = "bulk_final"


class EvidenceOut(ORM):
    id: int
    captured_at: datetime
    extracted: dict


class CheckOut(ORM):
    check_no: int
    title: str = ""
    instruction: str = ""
    critical: bool = False
    method: str = ""
    input_fields: list[str] = []
    evidence_count: int = 0
    evidence_ids: list[int] = []
    status: str
    ai_status: str | None
    confidence: float | None
    expected_value: str
    observed_value: str
    reason: str
    reviewer_comment: str
    corrective_action: str


class InspectionOut(ORM):
    id: int
    po_id: int
    po_number: str = ""
    asn_number: str | None = None
    article_number: str = ""
    vendor_code: str = ""
    color_name: str
    size: str
    inspection_type: str
    status: str
    parent_inspection_id: int | None
    created_at: datetime
    submitted_at: datetime | None
    decided_at: datetime | None
    decision_note: str
    progress: str = ""
    summary: str = ""
    checks: list[CheckOut] = []


class ReviewIn(BaseModel):
    status: Literal["pass", "fail", "manual_review"]
    comment: str = ""
    corrective_action: str = ""


class DecisionIn(BaseModel):
    decision: Literal["passed", "failed", "reinspection_required"]
    note: str = ""
    record_exception: bool = False   # required to pass with an open critical failure


class VideoTokenOut(BaseModel):
    url: str
    room: str
    token: str
