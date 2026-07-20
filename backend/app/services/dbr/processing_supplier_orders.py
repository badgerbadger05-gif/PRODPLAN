"""Canonical selection of supplier-order lines for the processing pipe.

The 1C business-operation kind is not synchronized. The strongest currently
available attribution is the preferred supplier stored on the processing item.
Callers must only pass items already classified as ``supply_type=processing``.

Items without a configured supplier, drafts, deleted documents, terminal
orders and fully received lines are excluded. A posted ordinary purchase from
the same configured contractor remains indistinguishable until operation kind
is added to the synchronized model.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...models import Item, Supplier, SupplierOrder, SupplierOrderItem
from ..supplier_order_status import state_is_terminal


def processing_order_rows(
    db: Session, item_ids: Iterable[int]
) -> list[tuple[SupplierOrderItem, SupplierOrder]]:
    ids = tuple(sorted({int(value) for value in item_ids}))
    if not ids:
        return []
    normalized_item_supplier = func.lower(func.trim(Item.supplier_ref1c))
    normalized_order_supplier = func.lower(func.trim(Supplier.supplier_ref1c))
    rows = (
        db.query(SupplierOrderItem, SupplierOrder)
        .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
        .join(Item, Item.item_id == SupplierOrderItem.item_id_ref)
        .join(Supplier, Supplier.supplier_id == SupplierOrder.supplier_id)
        .filter(
            SupplierOrderItem.item_id_ref.in_(ids),
            SupplierOrderItem.remaining_qty > 0,
            SupplierOrder.is_posted.is_(True),
            SupplierOrder.deletion_mark.is_(False),
            Item.supplier_ref1c.is_not(None),
            func.trim(Item.supplier_ref1c) != "",
            normalized_order_supplier == normalized_item_supplier,
        )
        .order_by(SupplierOrder.order_date.asc(), SupplierOrderItem.item_id.asc())
        .all()
    )
    # Python normalization is intentional: SQLite's lower() does not fold
    # Cyrillic, while the application-level state model handles case and ё.
    return [
        (line, order)
        for line, order in rows
        if not state_is_terminal(order.order_state_name)
    ]
