"""Canonical selection of supplier-order lines for the processing pipe.

Callers must only pass items already classified as ``supply_type=processing``.
Items without a configured supplier, drafts, deleted documents, terminal
orders, fully received lines and ordinary supplier orders are excluded.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...models import Item, Supplier, SupplierOrder, SupplierOrderItem
from ..supplier_order_status import state_is_terminal


PROCESSING_OPERATION_KEY = "8d96f6a2-9934-11eb-e39a-fa163e61326a"


def _is_processing_operation(order: SupplierOrder) -> bool:
    operation_key = str(order.operation_key or "").strip().lower()
    operation_name = "".join(
        char for char in str(order.operation_name or "").casefold() if char.isalnum()
    )
    return (
        operation_key == PROCESSING_OPERATION_KEY
        or operation_name == "заказнапереработку"
    )


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
        if _is_processing_operation(order)
        and not state_is_terminal(order.order_state_name)
    ]


def processing_history_rows(
    db: Session, item_ids: Iterable[int]
) -> list[tuple[SupplierOrderItem, SupplierOrder, Supplier]]:
    """Historical processing rows eligible for round-trip KPI calculation.

    Unlike :func:`processing_order_rows`, this intentionally includes terminal
    and fully received rows.  Supplier attribution and operation filtering stay
    identical to the live processing pipe, so ordinary purchases never leak
    into the KPI proxy.
    """
    ids = tuple(sorted({int(value) for value in item_ids}))
    if not ids:
        return []
    normalized_item_supplier = func.lower(func.trim(Item.supplier_ref1c))
    normalized_order_supplier = func.lower(func.trim(Supplier.supplier_ref1c))
    rows = (
        db.query(SupplierOrderItem, SupplierOrder, Supplier)
        .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
        .join(Item, Item.item_id == SupplierOrderItem.item_id_ref)
        .join(Supplier, Supplier.supplier_id == SupplierOrder.supplier_id)
        .filter(
            SupplierOrderItem.item_id_ref.in_(ids),
            SupplierOrder.is_posted.is_(True),
            SupplierOrder.deletion_mark.is_(False),
            Item.supplier_ref1c.is_not(None),
            func.trim(Item.supplier_ref1c) != "",
            normalized_order_supplier == normalized_item_supplier,
        )
        .order_by(SupplierOrder.order_date.asc(), SupplierOrderItem.item_id.asc())
        .all()
    )
    return [
        (line, order, supplier)
        for line, order, supplier in rows
        if _is_processing_operation(order)
    ]
