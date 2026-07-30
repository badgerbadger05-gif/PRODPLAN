"""Evidence adapter for open production-order (WIP) future supply.

This module is deliberately a *capture boundary*, not an MRP reader.  Its
``realized_qty`` comes only from the immutable physical Ledger prefix named by
an accepted generation.  The old production-journal counters are not evidence
and are intentionally never read here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Mapping

from sqlalchemy.orm import Session

from app import models
from app.services.planning_pool_resolver import require_mapped_destination

from .future_supply_capture import (
    FutureSupplyEvidence,
    future_supply_evidence_hash,
    replace_future_supply_capture,
)
from .physical_visibility import visible_sle_query


_MAKE_KINDS = frozenset({"assembly_in", "transfer_in"})
_SUCCESS = "success"


def _text(value: object) -> str:
    return str(value or "").strip()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    return None


def _evidence(**values: object) -> FutureSupplyEvidence:
    unsigned = FutureSupplyEvidence(**values)
    return FutureSupplyEvidence(
        **{**values, "source_content_hash": future_supply_evidence_hash(unsigned)}
    )


def _accepted_generation(db: Session, ledger_generation_id: int) -> models.LedgerGeneration:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise ValueError(f"LedgerGeneration {ledger_generation_id} not found")
    if generation.status != "accepted" or generation.cutoff is None:
        raise ValueError("WIP future supply requires an accepted LedgerGeneration with cutoff")
    return generation


def _pool_mapping(mapping: Mapping[str, str] | None) -> dict[str, str]:
    """Normalize the live planning-contour mapping resolved by orchestration."""
    return {
        _text(warehouse): _text(pool)
        for warehouse, pool in (mapping or {}).items()
        if _text(warehouse) and _text(pool)
    }


def _order_refs_by_recorder(db: Session) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for recorder, order_ref in db.query(
        models.StockRecorderPull.recorder_ref,
        models.StockRecorderPull.order_ref,
    ).filter(models.StockRecorderPull.order_ref.isnot(None)).all():
        if _text(recorder) and _text(order_ref):
            result[_text(recorder)].add(_text(order_ref))
    for (order_ref,) in db.query(models.ProductionOrder.order_ref1c).filter(
        models.ProductionOrder.order_ref1c.isnot(None)
    ).all():
        if _text(order_ref):
            # A direct order recorder is a supported, but lower-confidence,
            # exact route only if its product candidate is unique below.
            result[_text(order_ref)].add(_text(order_ref))
    return result


def collect_wip_future_supply_evidence(
    db: Session,
    ledger_generation_id: int,
    *,
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
    explicit_make_transfer_recorders: set[str] | None = None,
) -> list[FutureSupplyEvidence]:
    """Project ProductionProduct obligations and accepted make facts.

    Every production line is retained.  A missing contour destination, missing
    external line identity, or ambiguous receipt route becomes non-supply
    evidence with ``open=0`` when persisted; it is never converted into an
    invented zero fact.
    """
    generation = _accepted_generation(db, ledger_generation_id)
    pools_by_destination = _pool_mapping(planning_pool_by_warehouse)
    explicit_transfers = {_text(value) for value in (explicit_make_transfer_recorders or set()) if _text(value)}
    rows = db.query(models.ProductionProduct, models.ProductionOrder).join(
        models.ProductionOrder,
        models.ProductionOrder.order_id == models.ProductionProduct.order_id,
    ).order_by(models.ProductionProduct.product_id.asc()).all()
    by_product = {int(product.product_id): (product, order) for product, order in rows}
    by_order_item_char: dict[tuple[str, int, str], list[int]] = defaultdict(list)
    for product, order in rows:
        ref = _text(order.order_ref1c)
        if ref:
            by_order_item_char[(ref, int(product.item_id), _text(product.characteristic_ref1c))].append(
                int(product.product_id)
            )

    direct_links: dict[str, set[int]] = {}
    order_refs = _order_refs_by_recorder(db)
    realized: dict[int, Decimal] = defaultdict(Decimal)
    invalid_reasons: dict[int, set[str]] = defaultdict(set)
    evidence_kinds_by_product: dict[int, set[str]] = defaultdict(set)

    for sle in visible_sle_query(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=generation.cutoff,
    ):
        kind = _text(sle.movement_kind)
        qty = _decimal(sle.qty)
        if kind not in _MAKE_KINDS or qty <= 0:
            continue
        recorder = _text(sle.recorder_ref)
        candidates = set(direct_links.get(recorder, set()))
        route = "line-link" if candidates else ""
        if not candidates:
            refs = order_refs.get(recorder, set())
            if len(refs) == 1:
                candidates.update(by_order_item_char.get(
                    (next(iter(refs)), int(sle.item_id), _text(sle.characteristic_ref)), []
                ))
                route = "order-ref"
            elif len(refs) > 1:
                for ref in refs:
                    candidates.update(by_order_item_char.get(
                        (ref, int(sle.item_id), _text(sle.characteristic_ref)), []
                    ))
                route = "ambiguous-order-ref"

        # ``transfer_in`` is normally a material move.  It becomes make
        # evidence only when the caller supplies an explicit, audited semantic
        # classification for this recorder; warehouse flags cannot prove it.
        if kind == "transfer_in" and recorder not in explicit_transfers:
            for product_id in candidates:
                invalid_reasons[product_id].add("transfer_in has no explicit make semantics")
            continue
        if len(candidates) != 1:
            reason = "ambiguous make receipt linkage" if candidates else "unmatched make receipt linkage"
            for product_id in candidates:
                invalid_reasons[product_id].add(reason)
            continue
        product_id = next(iter(candidates))
        product_order = by_product.get(product_id)
        if product_order is None:
            continue
        product, _order = product_order
        if int(product.item_id) != int(sle.item_id) or _text(product.characteristic_ref1c) != _text(sle.characteristic_ref):
            invalid_reasons[product_id].add("receipt does not match production line item or characteristic")
            continue
        # The exact sync-link route is required when available; the order route
        # is allowed only after the unique candidate test above.
        if route == "ambiguous-order-ref":
            invalid_reasons[product_id].add("ambiguous order-ref make linkage")
            continue
        realized[product_id] += qty
        evidence_kinds_by_product[product_id].add(kind)

    for product_id, kinds in evidence_kinds_by_product.items():
        if len(kinds) > 1:
            invalid_reasons[product_id].add("ambiguous assembly and transfer make evidence")

    evidence: list[FutureSupplyEvidence] = []
    for product, order in rows:
        product_id = int(product.product_id)
        order_ref = _text(order.order_ref1c)
        line_ref = str(product.line_number) if product.line_number is not None else ""
        destination = _text(product.destination_warehouse_ref1c)
        planning_pool = pools_by_destination.get(destination, "")
        reason: str | None = None
        status = "exact"
        if not order_ref or not line_ref:
            status, reason = "rejected", "missing exact production order or line identity"
        elif not destination:
            status, reason = "rejected", "missing destination warehouse mapping"
        elif not planning_pool:
            planning_pool = require_mapped_destination(
                pools_by_destination,
                destination,
                source=f"wip_order:{order_ref}:{line_ref}",
            )
        elif invalid_reasons.get(product_id):
            status, reason = "rejected", "; ".join(sorted(invalid_reasons[product_id]))
        eta = _as_date(getattr(product.control_state, "planned_finish_date", None))
        if eta is None:
            eta = _as_date(order.order_date)
        values = dict(
            supply_kind="wip_order",
            item_id=int(product.item_id),
            characteristic_ref=_text(product.characteristic_ref1c),
            organization_ref="",
            planning_stock_pool=planning_pool,
            destination_warehouse_ref1c=destination,
            source_ref=order_ref or None,
            source_line_ref=line_ref or None,
            source_local_id=f"production_product:{product_id}",
            ordered_qty_at_cutoff=_decimal(product.quantity),
            realized_qty_at_cutoff=realized[product_id],
            eta_date=eta,
            source_state_key=_text(order.order_state_key) or "unknown",
            source_updated_at=order.updated_at or product.updated_at,
            capture_cutoff=generation.cutoff,
            evidence_status=status,
            reason=reason,
        )
        evidence.append(_evidence(**values))
    return evidence


def capture_wip_future_supply(
    db: Session,
    accepted_generation_id: int,
    target_generation_id: int,
    capture_batch_id: int,
    *,
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
    explicit_make_transfer_recorders: set[str] | None = None,
) -> dict[str, object]:
    """Persist the WIP projection through the shared future-supply capture core.

    This is intentionally a standalone stage helper.  A combined candidate
    builder must pass WIP and supplier evidence together to avoid replacing the
    other source kind.
    """
    source = _accepted_generation(db, accepted_generation_id)
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    if target is None or target.status != "building":
        raise ValueError("WIP future supply capture target must be a BUILDING LedgerGeneration")
    if (
        int(target.physical_import_batch_id) != int(source.physical_import_batch_id)
        or target.cutoff != source.cutoff
    ):
        raise ValueError("WIP future supply target must share the accepted physical prefix and cutoff")
    evidence = collect_wip_future_supply_evidence(
        db,
        accepted_generation_id,
        planning_pool_by_warehouse=planning_pool_by_warehouse,
        explicit_make_transfer_recorders=explicit_make_transfer_recorders,
    )
    return dict(replace_future_supply_capture(
        db, target_generation_id, capture_batch_id, evidence
    ))
