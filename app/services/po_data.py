"""PO master data the checks read from the database (senior's decisions of 2026-11-27).

- expected supplier code + batch for checks 17-20 ("a simple fixed query by PO, no NLP")
- ASN shipped quantity for check 14

Today these read the local tables `po_suppliers` and `asn_shipments`. When the SRM / Supabase tables are
confirmed, only the two functions below change (to a read-only fixed query); the checks stay the same.
"""
from dataclasses import dataclass

from app.models import PurchaseOrder


@dataclass(frozen=True)
class SupplierLot:
    supplier_code: str
    batch_no: str


def _norm(text: str | None) -> str:
    return "".join((text or "").split()).casefold()


def expected_supplier(po: PurchaseOrder, category: str | None) -> SupplierLot | None:
    """Supplier code + batch recorded for this PO and category, or None when nothing is recorded."""
    row = next((s for s in (getattr(po, "suppliers", None) or []) if s.category == category), None)
    return SupplierLot(row.supplier_code, row.batch_no or "") if row else None


def asn_quantity(po: PurchaseOrder) -> int | None:
    """Shipped quantity of the PO's ASN, or None when it is not known."""
    if not getattr(po, "asn_number", None):
        return None
    row = next((s for s in (getattr(po, "shipments", None) or []) if _norm(s.asn_number) == _norm(po.asn_number)), None)
    return row.shipped_qty if row else None
