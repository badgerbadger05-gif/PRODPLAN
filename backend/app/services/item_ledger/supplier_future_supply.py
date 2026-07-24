"""Project exported supplier orders into auditable future-supply evidence.

This boundary deliberately reads neither ``SupplierOrderItem.received_qty`` nor
any legacy proposal status/remaining projection. Realisation is reconstructed
from immutable, visible Stock Ledger rows for supplier-receipt operations.

Current export allocations do not yet retain a destination/pool.  They are
therefore preserved as rejected evidence (open quantity zero), rather than
silently becoming supply.  The small attribute accessor below is intentional:
once the exporter stamps those immutable fields, the same adapter can qualify
them without changing its identity or accounting rules.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

from .future_supply_capture import FutureSupplyEvidence, future_supply_evidence_hash
from .physical_visibility import visible_sle_query


def _text(value: object) -> str:
    return str(value or "").strip()


def _qty(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _evidence(**values: object) -> FutureSupplyEvidence:
    unsigned = FutureSupplyEvidence(**values)
    return FutureSupplyEvidence(
        **{**values, "source_content_hash": future_supply_evidence_hash(unsigned)}
    )


def _allocation_identity(allocation: object) -> tuple[str, int]:
    if isinstance(allocation, models.PurchaseExportLineAllocation):
        planned_purchase_id = getattr(allocation, "planned_purchase_id", None)
        if planned_purchase_id is None:
            return "", -1
        return "planned_purchase", int(planned_purchase_id)
    if isinstance(allocation, models.PurchaseExportObligationAllocation):
        reservation_id = getattr(allocation, "reservation_id", None)
        if reservation_id is None:
            return "", -1
        return "buy_reservation", int(reservation_id)
    return "", -1


def _allocation_item_id(allocation: object) -> int | None:
    direct_item_id = getattr(allocation, "item_id", None)
    if direct_item_id is not None:
        return int(direct_item_id)
    planned_purchase = getattr(allocation, "planned_purchase", None)
    if planned_purchase is not None and getattr(planned_purchase, "item_id", None) is not None:
        return int(planned_purchase.item_id)
    reservation = getattr(allocation, "reservation", None)
    if reservation is not None and getattr(reservation, "item_id", None) is not None:
        return int(reservation.item_id)
    return None


def _allocation_etas(allocation: object) -> tuple[object, ...]:
    if isinstance(allocation, models.PurchaseExportLineAllocation):
        purchase = getattr(allocation, "planned_purchase", None)
        if purchase is None:
            return tuple()
        need_date = getattr(purchase, "need_date", None)
        return (need_date,) if need_date is not None else tuple()
    if isinstance(allocation, models.PurchaseExportObligationAllocation):
        eta = getattr(allocation, "eta_date", None)
        return (eta,) if eta is not None else tuple()
    return tuple()


def _line_links(
    db: Session, generation_id: int, allocations: Iterable[object]
) -> tuple[models.SyncLink | None, str | None]:
    """Return the shared successful link or an explicit qualification error."""
    links: list[models.SyncLink] = []
    for allocation in allocations:
        source_doctype, source_id = _allocation_identity(allocation)
        link = db.query(models.SyncLink).filter(
            models.SyncLink.source_system == "PRODPLAN",
            models.SyncLink.source_doctype == source_doctype,
            models.SyncLink.source_id == int(source_id),
            models.SyncLink.target_entity == "Document_ЗаказПоставщику",
        ).one_or_none()
        generation_matches = (
            isinstance(allocation, models.PurchaseExportObligationAllocation)
            or int(link.ledger_generation_id or -1) == int(generation_id)
        ) if link is not None else False
        if (
            not source_doctype
            or link is None
            or str(link.status) != "success"
            or not generation_matches
            or _text(link.target_ref_key) != _text(getattr(allocation, "supplier_order_ref", ""))
        ):
            return None, "export_link_not_exact"
        links.append(link)
    if not links:
        return None, "export_link_not_exact"
    return max(links, key=lambda row: row.updated_at or datetime.min), None


def supplier_future_supply_evidence(
    db: Session,
    ledger_generation_id: int,
) -> tuple[FutureSupplyEvidence, ...]:
    """Build one supplier-order evidence row per exported 1C order line.

    The caller persists the result with ``replace_future_supply_capture``.  It
    owns neither transactions nor capture batches.
    """
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None or generation.cutoff is None:
        raise ValueError("supplier future supply requires a generation with cutoff")

    grouped: dict[tuple[str, str], list[object]] = defaultdict(list)
    for row in db.query(models.PurchaseExportLineAllocation).filter_by(
        ledger_generation_id=int(generation.id)
    ).all():
        grouped[(_text(row.supplier_order_ref), _text(row.supplier_order_line_no))].append(row)
    # BUY allocations are immutable order-line facts.  They survive Ledger
    # generations; only their receipts/returns are evaluated at this
    # generation's physical cutoff below.
    for row in (
        db.query(models.PurchaseExportObligationAllocation)
        .join(
            models.LedgerGeneration,
            models.LedgerGeneration.id
            == models.PurchaseExportObligationAllocation.ledger_generation_id,
        )
        .filter(
            models.PurchaseExportObligationAllocation.reservation_id.is_not(None),
            models.LedgerGeneration.cutoff <= generation.cutoff,
        )
        .all()
    ):
        grouped[(_text(row.supplier_order_ref), _text(row.supplier_order_line_no))].append(row)

    visible_ids = visible_sle_query(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=generation.cutoff,
    ).with_entities(models.StockLedgerEntry.id).subquery()
    realized_by_line: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for order_ref, line_no, qty in db.query(
        models.StockLedgerSupplierReceiptProvenance.supplier_order_ref,
        models.StockLedgerSupplierReceiptProvenance.supplier_order_line_no,
        models.StockLedgerEntry.qty,
    ).join(
        models.StockLedgerEntry,
        models.StockLedgerEntry.id
        == models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id,
    ).filter(
        models.StockLedgerSupplierReceiptProvenance.ledger_generation_id == int(generation.id),
        models.StockLedgerSupplierReceiptProvenance.operation_kind.in_(
            ("supplier_receipt", "correction", "supplier_return")
        ),
        models.StockLedgerEntry.id.in_(select(visible_ids.c.id)),
    ).all():
        realized_by_line[(_text(order_ref), _text(line_no))] += _qty(qty)

    result: list[FutureSupplyEvidence] = []
    for (order_ref, line_no), rows in sorted(grouped.items()):
        ordered = sum((_qty(row.allocated_qty) for row in rows), Decimal("0"))
        item_ids = {
            int(item_id)
            for row in rows
            if (item_id := _allocation_item_id(row)) is not None
        }
        item_id = next(iter(item_ids), None)
        # These fields are deliberately read from allocation, not inferred from
        # a warehouse/default/configuration.  Older rows have neither attribute.
        pools = {_text(getattr(row, "planning_stock_pool", "")) for row in rows}
        destinations = {_text(getattr(row, "destination_warehouse_ref1c", "")) for row in rows}
        etas = {
            eta
            for row in rows
            for eta in _allocation_etas(row)
            if eta is not None
        }
        link, link_error = _line_links(db, generation.id, rows)
        reason: str | None = None
        status = "exact"
        if len(item_ids) != 1 or item_id is None:
            status, reason = "rejected", "item_not_singleton"
        elif not destinations or "" in destinations or len(destinations) != 1:
            status, reason = "rejected", "destination_not_stamped"
        elif not pools or "" in pools or len(pools) != 1:
            status, reason = "rejected", "planning_pool_not_stamped"
        elif link_error:
            status, reason = "rejected", link_error

        values: dict[str, object] = {
            "supply_kind": "supplier_order",
            "item_id": item_id,
            "planning_stock_pool": next(iter(pools), ""),
            "destination_warehouse_ref1c": next(iter(destinations), ""),
            "source_ref": order_ref or None,
            "source_line_ref": line_no or None,
            "source_local_id": ",".join(str(row.id) for row in sorted(rows, key=lambda row: row.id)),
            "ordered_qty_at_cutoff": ordered,
            "realized_qty_at_cutoff": realized_by_line[(order_ref, line_no)],
            "eta_date": min(etas) if etas else None,
            "source_state_key": "exported" if link else "",
            "source_updated_at": link.updated_at if link else None,
            "capture_cutoff": generation.cutoff,
            "evidence_status": status,
            "reason": reason,
        }
        result.append(_evidence(**values))
    return tuple(result)
