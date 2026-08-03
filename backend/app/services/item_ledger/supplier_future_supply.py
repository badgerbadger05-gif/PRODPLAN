"""Project supplier orders into auditable future-supply evidence.

This boundary deliberately reads neither ``SupplierOrderItem.received_qty`` nor
any legacy proposal status/remaining projection. Realisation is reconstructed
from immutable, visible Stock Ledger rows for supplier-receipt operations.

The 1C mirror is the source document for lines created either directly in 1C or
through PRODPLAN.  Immutable PRODPLAN allocations remain provenance and are
merged into that same external line identity; they are never emitted as a
second supply row.  Missing or conflicting item, state, destination, pool, link
or quantity evidence is retained as rejected evidence (open quantity zero).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.services.supplier_order_status import SupplyPhase, phase_for_state

from .future_supply_capture import FutureSupplyEvidence, future_supply_evidence_hash
from .physical_visibility import visible_sle_query


def _text(value: object) -> str:
    return str(value or "").strip()


def _qty(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _after_cutoff(value: object, cutoff: datetime) -> bool:
    if not isinstance(value, datetime):
        return False
    left = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    right = cutoff if cutoff.tzinfo is not None else cutoff.replace(tzinfo=timezone.utc)
    return left > right


def _pool_mapping(mapping: Mapping[str, str] | None) -> dict[str, str]:
    """Normalize the live-contour mapping sealed into the refresh manifest."""
    return {
        _text(warehouse): _text(pool)
        for warehouse, pool in (mapping or {}).items()
        if _text(warehouse) and _text(pool)
    }


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
    *,
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
) -> tuple[FutureSupplyEvidence, ...]:
    """Build one supplier-order evidence row per external 1C order line.

    The caller persists the result with ``replace_future_supply_capture``.  It
    owns neither transactions nor capture batches.
    """
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None or generation.cutoff is None:
        raise ValueError("supplier future supply requires a generation with cutoff")

    pools_by_destination = _pool_mapping(planning_pool_by_warehouse)
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

    mirrored: dict[
        tuple[str, str], tuple[models.SupplierOrderItem, models.SupplierOrder]
    ] = {}
    for line, order in (
        db.query(models.SupplierOrderItem, models.SupplierOrder)
        .join(
            models.SupplierOrder,
            models.SupplierOrder.order_id == models.SupplierOrderItem.order_id,
        )
        .order_by(
            models.SupplierOrder.order_ref1c.asc(),
            models.SupplierOrderItem.line_number.asc(),
            models.SupplierOrderItem.item_id.asc(),
        )
        .all()
    ):
        identity = (_text(order.order_ref1c), _text(line.line_number))
        # Keep malformed mirrors auditable without letting two local rows
        # collapse into one external identity.
        if not identity[0]:
            identity = (f"local-order:{int(order.order_id)}", identity[1])
        if not identity[1]:
            identity = (identity[0], f"local-line:{int(line.item_id)}")
        if identity in mirrored:
            raise ValueError("supplier-order mirror contains duplicate external line identity")
        mirrored[identity] = (line, order)

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
    all_identities = sorted(set(grouped) | set(mirrored))
    for order_ref, line_no in all_identities:
        rows = grouped.get((order_ref, line_no), [])
        mirror = mirrored.get((order_ref, line_no))
        mirror_line, mirror_order = mirror if mirror is not None else (None, None)
        external_order_ref = (
            _text(mirror_order.order_ref1c)
            if mirror_order is not None
            else order_ref
        )
        external_line_ref = (
            _text(mirror_line.line_number)
            if mirror_line is not None
            else line_no
        )

        allocation_ordered = sum(
            (_qty(row.allocated_qty) for row in rows), Decimal("0")
        )
        allocation_item_ids = {
            int(item_id)
            for row in rows
            if (item_id := _allocation_item_id(row)) is not None
        }
        allocation_pools = {
            value
            for row in rows
            if (value := _text(getattr(row, "planning_stock_pool", "")))
        }
        allocation_destinations = {
            value
            for row in rows
            if (value := _text(getattr(row, "destination_warehouse_ref1c", "")))
        }
        allocation_etas = {
            eta
            for row in rows
            for eta in _allocation_etas(row)
            if eta is not None
        }

        mirror_item_id = (
            int(mirror_line.item_id_ref) if mirror_line is not None else None
        )
        item_id = mirror_item_id or next(iter(allocation_item_ids), None)
        ordered = (
            _qty(mirror_line.quantity)
            if mirror_line is not None
            else allocation_ordered
        )
        destination = (
            _text(mirror_line.destination_warehouse_ref1c)
            if mirror_line is not None
            else next(iter(allocation_destinations), "")
        )
        planning_pool = pools_by_destination.get(destination, "")
        eta = (
            _date(mirror_line.delivery_date)
            if mirror_line is not None
            else min(allocation_etas) if allocation_etas else None
        )

        link, link_error = (
            _line_links(db, generation.id, rows)
            if rows
            else (None, None)
        )
        raw_state = (
            _text(mirror_order.order_state_name)
            if mirror_order is not None
            else "exported" if link else ""
        )[:64]
        characteristic_ref = (
            _text(mirror_line.characteristic_ref1c)
            if mirror_line is not None
            else ""
        )
        phase = (
            phase_for_state(mirror_order.order_state_name)
            if mirror_order is not None
            else None
        )

        reason: str | None = None
        status = "exact"
        if item_id is None:
            status, reason = "rejected", "item_not_singleton"
        elif not external_order_ref or not external_line_ref:
            status, reason = "rejected", "source_identity_missing"
        elif len(allocation_item_ids) > 1 or (
            mirror_item_id is not None
            and allocation_item_ids
            and allocation_item_ids != {mirror_item_id}
        ):
            status, reason = "rejected", "item_conflicts_with_export_provenance"
        elif mirror_order is not None and bool(mirror_order.deletion_mark):
            status, reason = "rejected", "supplier_order_deleted"
        elif mirror_order is not None and (
            _after_cutoff(mirror_order.order_date, generation.cutoff)
            or _after_cutoff(mirror_order.created_at, generation.cutoff)
            or (
                mirror_line is not None
                and _after_cutoff(mirror_line.created_at, generation.cutoff)
            )
        ):
            status, reason = "rejected", "supplier_order_created_after_capture_cutoff"
        elif mirror_order is not None and phase is SupplyPhase.TERMINAL:
            status, reason = "rejected", "supplier_order_terminal"
        elif mirror_order is not None and phase is SupplyPhase.NO_GOODS:
            status, reason = "rejected", "supplier_order_phase_no_goods"
        elif mirror_order is not None and phase is SupplyPhase.UNKNOWN:
            status, reason = "rejected", "supplier_order_state_unknown"
        elif characteristic_ref:
            # The current MRP pool key intentionally collapses characteristics.
            # Counting a characteristic-specific supplier line against it would
            # cover another variant, so retain the line as non-supply until the
            # canonical pool key is widened end-to-end.
            status, reason = "rejected", "characteristic_not_supported_by_mrp_pool"
        elif eta is None:
            status, reason = "rejected", "supplier_order_eta_missing"
        elif not destination:
            status, reason = "rejected", "destination_not_stamped"
        elif len(allocation_destinations) > 1 or (
            allocation_destinations and allocation_destinations != {destination}
        ):
            status, reason = "rejected", "destination_conflicts_with_export_provenance"
        elif len(allocation_pools) > 1 or (
            allocation_pools and planning_pool and allocation_pools != {planning_pool}
        ):
            # Ordered before the contour test so export provenance that already
            # disagrees with itself is not masked by an unmapped destination.
            status, reason = "rejected", "planning_pool_conflicts_with_export_provenance"
        elif not planning_pool:
            # A destination outside the live contour disqualifies this line
            # only; one stray warehouse must never abort the whole refresh.
            status, reason = "rejected", "planning_pool_not_mapped"
        elif mirror_line is not None and rows and allocation_ordered != ordered:
            status, reason = "rejected", "quantity_conflicts_with_export_provenance"
        elif mirror_line is None and link_error:
            status, reason = "rejected", link_error

        local_ids = []
        if mirror_line is not None:
            local_ids.append(f"supplier_order_item:{int(mirror_line.item_id)}")
        local_ids.extend(
            f"{type(row).__name__}:{int(row.id)}"
            for row in sorted(rows, key=lambda row: (type(row).__name__, int(row.id)))
        )
        values: dict[str, object] = {
            "supply_kind": "supplier_order",
            "item_id": item_id,
            "characteristic_ref": characteristic_ref,
            "planning_stock_pool": planning_pool,
            "destination_warehouse_ref1c": destination,
            "source_ref": external_order_ref or None,
            "source_line_ref": external_line_ref or None,
            "source_local_id": ",".join(local_ids),
            "ordered_qty_at_cutoff": ordered,
            "realized_qty_at_cutoff": realized_by_line[(order_ref, line_no)],
            "eta_date": eta,
            "source_state_key": raw_state,
            # Mirror timestamps are synchronization metadata, not versioned 1C
            # business facts. They neither gate cutoff visibility nor enter
            # the evidence hash.
            "source_updated_at": None,
            "capture_cutoff": generation.cutoff,
            "evidence_status": status,
            "reason": reason,
        }
        result.append(_evidence(**values))
    return tuple(result)
