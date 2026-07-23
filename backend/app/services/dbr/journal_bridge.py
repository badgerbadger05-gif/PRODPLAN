"""Project manufacturing DBR feeder signals into the production journal.

This is deliberately a projection, not a second work queue: one DBR signal
owns at most one local ``ProductionProduct`` through ``source_dbr_signal_id``.
The caller owns the transaction and may call this repeatedly after any signal
refresh.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from ...models import (
    DbrFeederSignal,
    DbrSupermarketPosition,
    DefaultSpecification,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
)
from ..workshop_resolution import resolve_workshop_for_spec
from ..replenishment import REPLENISHMENT_FLOW_PRODUCTION, classify_replenishment_flow


_ACTIVE = {"Open", "Diagnostic", "Order Created", "In Work"}


def _spec_id(db: Session, item_id: int) -> int | None:
    row = (
        db.query(DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id == int(item_id))
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(row.spec_id) if row else None


def _is_manufacturing_signal(
    signal: DbrFeederSignal,
    positions: dict[int, DbrSupermarketPosition],
    spec_id: int | None,
) -> bool:
    """Exclude procurement and external-processing signals from mechshop work."""
    if signal.supermarket_position_id is not None:
        position = positions.get(int(signal.supermarket_position_id))
        return bool(position and position.supply_type == "manufacture")
    # Chain signals have no supermarket position.  A specification alone is
    # insufficient: purchased and toll-processed items may also have one.
    # Use the same replenishment classifier as the rest of planning.
    return (
        signal.signal_type == "Цепочка"
        and spec_id is not None
        and classify_replenishment_flow(
            getattr(signal.item, "replenishment_method", None)
        )
        == REPLENISHMENT_FLOW_PRODUCTION
    )


def _line_state(db: Session, product_id: int) -> ProductionOrderLineState | None:
    return (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == int(product_id))
        .one_or_none()
    )


def sync_journal_rows(db: Session, signals: Iterable[DbrFeederSignal] | None = None) -> dict[str, int]:
    """Idempotently upsert/cancel local journal rows for DBR manufacturing signals."""
    signal_rows = list(signals) if signals is not None else db.query(DbrFeederSignal).all()
    position_ids = {int(row.supermarket_position_id) for row in signal_rows if row.supermarket_position_id is not None}
    positions = {
        int(row.id): row
        for row in db.query(DbrSupermarketPosition).filter(DbrSupermarketPosition.id.in_(position_ids or [-1])).all()
    }
    existing = {
        int(product.source_dbr_signal_id): product
        for product in db.query(ProductionProduct)
        .filter(ProductionProduct.source_dbr_signal_id.isnot(None))
        .all()
    }
    created = updated = cancelled = skipped = 0
    for signal in signal_rows:
        spec_id = _spec_id(db, int(signal.item_id))
        if not _is_manufacturing_signal(signal, positions, spec_id):
            skipped += 1
            continue
        product = existing.get(int(signal.id))
        if signal.status == "Cancelled":
            if product is not None and not signal.one_c_order_ref:
                state = _line_state(db, int(product.product_id))
                if state is None:
                    state = ProductionOrderLineState(product_id=product.product_id, status="cancelled")
                    db.add(state)
                else:
                    state.status = "cancelled"
                cancelled += 1
            continue
        if signal.status not in _ACTIVE:
            continue
        quantity = float(signal.suggested_qty or 0)
        if product is None:
            order = ProductionOrder(
                order_number=f"DBR-S{int(signal.id)}",
                order_date=datetime.now(),
                source="dbr",
                deletion_mark=False,
            )
            product = ProductionProduct(
                order=order,
                item_id=int(signal.item_id),
                line_number=1,
                destination_warehouse_ref1c=signal.warehouse_ref1c,
                quantity=quantity,
                produced_qty=0,
                remaining_qty=quantity,
                spec_id=spec_id,
                source_dbr_signal_id=int(signal.id),
            )
            db.add_all([order, product])
            db.flush()
            existing[int(signal.id)] = product
            created += 1
        else:
            # Refreshing an advisory signal must not rewrite work already
            # performed by a master or facts imported from 1C.
            if not signal.one_c_order_ref and float(product.produced_qty or 0) == 0:
                changed = (
                    product.order.source != "dbr"
                    or product.order.order_number != f"DBR-S{int(signal.id)}"
                    or product.destination_warehouse_ref1c != signal.warehouse_ref1c
                    or product.spec_id != spec_id
                    or float(product.quantity or 0) != quantity
                    or float(product.remaining_qty or 0) != quantity
                )
                product.order.source = "dbr"
                product.order.order_number = f"DBR-S{int(signal.id)}"
                product.destination_warehouse_ref1c = signal.warehouse_ref1c
                product.spec_id = spec_id
                product.quantity = quantity
                product.remaining_qty = quantity
                if changed:
                    updated += 1
        state = _line_state(db, int(product.product_id))
        if state is None:
            workshop_id = resolve_workshop_for_spec(db, spec_id) if spec_id else None
            state = ProductionOrderLineState(
                product_id=product.product_id,
                status="shortage",
                planned_start_date=signal.need_date,
                planned_finish_date=signal.required_date or signal.need_date,
                workshop_id=workshop_id,
                workshop_id_source="auto" if workshop_id else None,
            )
            db.add(state)
        elif state.status == "cancelled" and signal.status in {"Open", "Diagnostic"}:
            # A cancelled local proposal may be reopened by a reappearing DBR
            # signal, but no other manual journal state is overwritten.
            state.status = "shortage"
        if not signal.one_c_order_ref and float(product.produced_qty or 0) == 0:
            state.planned_start_date = signal.need_date
            state.planned_finish_date = signal.required_date or signal.need_date
    db.flush()
    return {"created": created, "updated": updated, "cancelled": cancelled, "skipped": skipped}
