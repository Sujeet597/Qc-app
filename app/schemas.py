import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


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


class POCreate(BaseModel):
    po_number: str
    asn_number: str | None = None
    vendor_code: str
    article_number: str
    style_number: str
    color_name: str
    mc: str
    barcode_number: str
    taffeta_number: str
    sizes: list[str]
    weight_target_g: float | None = None
    weight_tolerance_g: float | None = None
    gsm_target: float | None = None
    gsm_tolerance: float | None = None
    size_chart: dict[str, dict[str, tuple[float, float]]] = {}
    packing_norms: dict = {}
    order_qty: int = 0

    @field_validator("size_chart", mode="before")
    @classmethod
    def _valid_size_chart(cls, chart: object) -> object:
        if not isinstance(chart, dict):
            raise ValueError("size_chart must be a JSON object")
        for size, points in chart.items():
            if not isinstance(size, str) or not isinstance(points, dict):
                raise ValueError("size_chart must map size to {point: [target, tolerance]}")
            for point, pair in points.items():
                if not isinstance(point, str) or not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ValueError(f"size_chart[{size!r}][{point!r}] must be [target, tolerance]")
                t, tol = pair
                if isinstance(t, bool) or isinstance(tol, bool) or not isinstance(t, (int, float)) or not isinstance(tol, (int, float)):
                    raise ValueError(f"size_chart[{size!r}][{point!r}] values must be numbers")
                if not (math.isfinite(t) and math.isfinite(tol)) or tol < 0:
                    raise ValueError(f"size_chart[{size!r}][{point!r}] must be finite numbers with tolerance >= 0")
        return chart


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
