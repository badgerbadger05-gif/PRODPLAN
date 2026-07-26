"""Generation-scoped persistence adapter for historical Item Ledger replay."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    LedgerBuildBatch,
    LedgerGeneration,
    MrpRequirementBucket,
    ReservationEntry,
    StockLedgerEntry,
)
from app import models
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C

from .historical_replay_core import Fact, Reserve, allocate_historical_facts
from .physical_visibility import visible_sles_for_generation
from .reconcile import contour_warehouse_refs
from .reservation import append_realization_event, fold_reservation_entry


_ALGORITHM_VERSION = "historical-replay-persistence/1"
_SAFE_REALIZATION_KINDS = frozenset({"assembly_in"})
_IGNORED_FACT_KINDS = frozenset({"assembly_out", "writeoff", "receipt", "expense"})
_UNRESOLVED_POOL_PREFIX = "__unresolved_pool__"
_NO_POOL_SENTINEL_PREFIX = "__no_pool__"


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def bucket_capacity_for_mode(
    bucket: MrpRequirementBucket,
    mode: str,
) -> Decimal:
    mode_key = str(mode or "").lower()
    if mode_key == "make":
        return max(_decimal(bucket.net_qty), Decimal("0"))
    if mode_key == "buy":
        return max(_decimal(bucket.net_qty), Decimal("0"))
    raise ValueError(f"unsupported realization mode for bucket capacity: {mode}")


def _checksum(rows: list[dict[str, Any]]) -> str:
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fact_mode(row: StockLedgerEntry) -> str:
    if str(row.movement_kind or "") != "assembly_in":
        raise ValueError("only positive production replenishment enters replay")
    return "make"


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def _replay_lower_bound(
    generation: LedgerGeneration,
    replay_from: datetime | None,
) -> datetime:
    if replay_from is not None:
        return _as_datetime(replay_from)  # type: ignore[return-value]
    watermarks = dict(generation.source_watermarks or {})
    batch_watermarks = dict(
        generation.physical_import_batch.source_watermarks or {}
    ) if generation.physical_import_batch is not None else {}
    for key in ("replay_from", "from_exclusive", "anchor_at"):
        value = _as_datetime(watermarks.get(key) or batch_watermarks.get(key))
        if value is not None:
            return value
    raise ValueError("historical replay requires explicit replay_from lower bound")


def _identity_for_sle(
    db: Session,
    row: StockLedgerEntry,
) -> tuple[int | None, str | None, bool]:
    """Resolve recorder to one unambiguous production requirement/order."""
    recorder = str(row.recorder_ref or "").strip()
    if not recorder:
        return None, None, False
    order_refs = {
        str(value).strip()
        for (value,) in db.query(models.StockRecorderPull.order_ref).filter(
            models.StockRecorderPull.recorder_ref == recorder,
            models.StockRecorderPull.order_ref.isnot(None),
        ).all()
        if str(value or "").strip()
    }
    if db.query(models.ProductionOrder.order_id).filter(
        models.ProductionOrder.order_ref1c == recorder
    ).first():
        order_refs.add(recorder)

    # Relevant successful export links may identify the local source document.
    links = db.query(models.SyncLink).filter(
        models.SyncLink.target_ref_key == recorder,
        models.SyncLink.status == "success",
        models.SyncLink.source_doctype == "material_issue",
    ).all()
    order_ids: set[int] = set()
    for link in links:
        source = db.get(models.ProductionMaterialIssue, int(link.source_id))
        if source is not None:
            order_ids.add(int(source.order_id))
    if order_refs:
        order_ids.update(
            int(value)
            for (value,) in db.query(models.ProductionOrder.order_id).filter(
                models.ProductionOrder.order_ref1c.in_(sorted(order_refs))
            ).all()
        )
    if len(order_ids) != 1:
        return None, None, len(order_ids) > 1
    order_id = next(iter(order_ids))
    order = db.get(models.ProductionOrder, order_id)
    candidates = {
        int(req_id)
        for (req_id,) in db.query(models.ProductionProduct.source_mrp_requirement_id).filter(
            models.ProductionProduct.order_id == order_id,
            models.ProductionProduct.item_id == int(row.item_id),
            models.ProductionProduct.source_mrp_requirement_id.isnot(None),
        ).all()
    }
    if len(candidates) != 1:
        return (
            None,
            None if len(candidates) > 1 else (str(order.order_ref1c or "") or None),
            len(candidates) > 1,
        )
    return next(iter(candidates)), str(order.order_ref1c or "") or None, False


def run_historical_replay(
    db: Session,
    ledger_generation_id: int,
    *,
    replay_from: datetime | None = None,
    run_ids: tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """Replay one explicit BUILDING generation and persist only scoped output."""
    generation = db.get(LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise ValueError(f"LedgerGeneration {ledger_generation_id} not found")
    if generation.status != "building":
        raise ValueError("historical replay requires an explicit BUILDING LedgerGeneration")
    if generation.cutoff is None:
        raise ValueError("historical replay requires generation cutoff")
    if generation.physical_import_batch_id is None:
        raise ValueError("historical replay requires physical_import_batch_id")
    lower_bound = _replay_lower_bound(generation, replay_from)

    entries_query = db.query(ReservationEntry).filter(
        ReservationEntry.ledger_generation_id == generation.id,
        ReservationEntry.lifecycle_status == "active",
    )
    if run_ids is not None:
        scoped_ids = tuple(sorted({int(value) for value in run_ids}))
        entries_query = (
            entries_query.filter(ReservationEntry.run_id.in_(scoped_ids))
            if scoped_ids
            else entries_query.filter(False)
        )
    entries = entries_query.order_by(ReservationEntry.id.asc()).all()
    requirement_ids = [int(row.requirement_id) for row in entries]
    buckets_by_requirement: dict[int, list[MrpRequirementBucket]] = {}
    if requirement_ids:
        for bucket in (
            db.query(MrpRequirementBucket)
            .filter(MrpRequirementBucket.requirement_id.in_(requirement_ids))
            .order_by(
                MrpRequirementBucket.requirement_id.asc(),
                MrpRequirementBucket.bucket_date.asc(),
                MrpRequirementBucket.id.asc(),
            )
            .all()
        ):
            buckets_by_requirement.setdefault(int(bucket.requirement_id), []).append(bucket)

    order_refs_by_requirement: dict[int, tuple[str, ...]] = {}
    if requirement_ids:
        rows = (
            db.query(
                models.ProductionProduct.source_mrp_requirement_id,
                models.ProductionOrder.order_ref1c,
            )
            .join(
                models.ProductionOrder,
                models.ProductionOrder.order_id == models.ProductionProduct.order_id,
            )
            .filter(
                models.ProductionProduct.source_mrp_requirement_id.in_(requirement_ids),
                models.ProductionOrder.order_ref1c.isnot(None),
            )
            .all()
        )
        refs: dict[int, set[str]] = {}
        for requirement_id, order_ref in rows:
            refs.setdefault(int(requirement_id), set()).add(str(order_ref))
        order_refs_by_requirement = {
            requirement_id: tuple(sorted(values))
            for requirement_id, values in refs.items()
        }

    reserves: list[Reserve] = []
    entry_by_core_id: dict[str, ReservationEntry] = {}
    pools_by_key: dict[tuple[int, str, str], set[str]] = {}
    contour_refs = contour_warehouse_refs(db)
    has_warehouse_policy = db.query(models.StockWarehouse).count() > 0
    visible_candidates = [
        row
        for row in visible_sles_for_generation(db, int(generation.id))
        if str(row.movement_kind or "") in (
            _SAFE_REALIZATION_KINDS | _IGNORED_FACT_KINDS
        )
        and _decimal(row.qty) != 0
    ]
    candidate_rows = [row for row in visible_candidates if row.posting_at > lower_bound]
    excluded_pre_replay = len(visible_candidates) - len(candidate_rows)
    physical_rows = [
        row for row in candidate_rows
        if str(row.movement_kind or "") in _SAFE_REALIZATION_KINDS
    ]
    ignored_rows = [
        row for row in candidate_rows
        if str(row.movement_kind or "") in _IGNORED_FACT_KINDS
    ]
    for row in entries:
        if row.run_id is None:
            raise ValueError(f"reservation {row.id} has no run lineage")
        core_id = str(int(row.id))
        buckets = buckets_by_requirement.get(int(row.requirement_id), [])
        bucket = buckets[0] if buckets else None
        reserve = Reserve(
            reserve_id=core_id,
            item_id=int(row.item_id),
            mode=str(row.realization_mode),  # validated by pure core
            reserved_qty=_decimal(row.replenishment_required_qty),
            due_date=row.priority_period_to,
            plan_period_from=row.priority_period_from,
            plan_period_to=row.priority_period_to,
            run_id=int(row.run_id),
            requirement_id=int(row.requirement_id),
            bucket_date=bucket.bucket_date if bucket else None,
            bucket_id=int(bucket.id) if bucket else None,
            characteristic_ref=str(row.characteristic_ref or ""),
            organization_ref="",
            planning_stock_pool=str(row.planning_stock_pool or ""),
            order_refs=order_refs_by_requirement.get(int(row.requirement_id), ()),
        )
        reserves.append(reserve)
        entry_by_core_id[core_id] = row
        pools_by_key.setdefault(
            (
                reserve.item_id,
                reserve.organization_ref,
                reserve.mode,
            ),
            set(),
        ).add(reserve.planning_stock_pool)
    facts: list[Fact] = []
    excluded_make_facts = 0
    excluded_make_qty = Decimal("0")
    excluded_make_samples: list[dict[str, str | int]] = []
    sle_by_core_id: dict[str, StockLedgerEntry] = {}
    mode_has_pools = set(
        (item_id, mode) for item_id, _org, mode in pools_by_key.keys()
    )
    ambiguous_pool_facts = 0
    ambiguous_identity_facts = 0
    legacy_identity_collapsed_pool_facts = 0
    for row in physical_rows:
        if str(row.organization_ref or "").strip() != DEFAULT_ORGANIZATION_REF1C:
            ignored_rows.append(row)
            continue
        mode = _fact_mode(row)
        if (
            mode == "make"
            and (
                not has_warehouse_policy
                or not str(row.warehouse_ref1c or "").strip()
                or str(row.warehouse_ref1c).strip() in contour_refs
            )
        ):
            excluded_make_facts += 1
            excluded_make_qty += abs(_decimal(row.qty))
            excluded_make_samples.append({
                "sle_id": int(row.id),
                "warehouse_ref1c": str(row.warehouse_ref1c or ""),
                "reason": (
                    "warehouse_policy_missing"
                    if not has_warehouse_policy
                    else "ambiguous"
                    if not str(row.warehouse_ref1c or "").strip()
                    else "contour"
                ),
            })
            continue
        exact_key = (
            int(row.item_id),
            "",
            mode,
        )
        pools = pools_by_key.get(exact_key, set())
        fact_characteristic = ""
        if len(pools) == 1:
            pool = next(iter(pools))
        elif (
            not pools
            and (int(row.item_id), mode) not in mode_has_pools
        ):
            pool = f"{_NO_POOL_SENTINEL_PREFIX}:{int(row.item_id)}:{mode}"
        else:
            pool = f"{_UNRESOLVED_POOL_PREFIX}:{row.id}"
            ambiguous_pool_facts += 1
        core_id = str(int(row.id))
        requirement_id, order_ref, ambiguous_identity = _identity_for_sle(db, row)
        if ambiguous_identity:
            ambiguous_identity_facts += 1
        facts.append(Fact(
            fact_id=core_id,
            item_id=int(row.item_id),
            mode=mode,  # type: ignore[arg-type]
            qty=abs(_decimal(row.qty)),
            posting_at=row.posting_at,
            characteristic_ref=fact_characteristic,
            organization_ref="",
            planning_stock_pool=pool,
            requirement_id=requirement_id,
            order_ref=order_ref,
        ))
        sle_by_core_id[core_id] = row

    result = allocate_historical_facts(facts, reserves)
    cycle_id = f"historical-replay:g{generation.id}"
    inserted_events = 0
    allocation_rows_for_checksum: list[dict[str, Any]] = []
    for allocation in result.allocations:
        entry = entry_by_core_id[allocation.reserve_id]
        sle = sle_by_core_id[allocation.fact_id]
        idempotency_key = f"hist:g{generation.id}:sle{sle.id}:r{entry.id}"
        mode = _fact_mode(sle)
        if append_realization_event(
            db,
            entry,
            realized_delta=allocation.qty,
            sle_id=int(sle.id),
            fact_ref=str(sle.recorder_ref or f"sle:{sle.id}"),
            fact_line_ref=str(sle.line_no or ""),
            match_rule="fifo" if allocation.match_rule == "fifo" else "pegged",
            cycle_id=cycle_id,
            idempotency_key=idempotency_key,
            event_at=sle.posting_at,
        ):
            inserted_events += 1

        allocation_rows_for_checksum.append({
            "sle_id": int(sle.id),
            "reservation_id": int(entry.id),
            "requirement_id": int(entry.requirement_id),
            "qty": str(allocation.qty),
            "rule": allocation.match_rule,
        })

    for entry in entries:
        fold_reservation_entry(db, int(entry.id))

    input_rows = [
        {
            "sle_id": int(row.id),
            "qty": str(abs(_decimal(row.qty))),
            "kind": row.movement_kind,
            "eligible": str(row.movement_kind or "") in _SAFE_REALIZATION_KINDS,
        }
        for row in candidate_rows
    ]
    metrics = {
        "facts": len(facts),
        "ignored_facts": len(ignored_rows),
        "ignored_fact_qty": str(sum((abs(_decimal(row.qty)) for row in ignored_rows), Decimal("0"))),
        "reservations": len(reserves),
        "allocations": len(result.allocations),
        "events_inserted": inserted_events,
        "execution_allocations_inserted": 0,
        "fact_qty": str(result.fact_qty),
        "allocated_qty": str(result.allocated_qty),
        "surplus_qty": str(result.surplus_qty),
        "surplus_facts": len(result.surplus),
        "ambiguous_pool_facts": ambiguous_pool_facts,
        "ambiguous_identity_facts": ambiguous_identity_facts,
        "legacy_identity_collapsed_pool_facts": (
            legacy_identity_collapsed_pool_facts
        ),
        "excluded_make_facts": excluded_make_facts,
        "excluded_make_qty": str(excluded_make_qty),
        "excluded_make_samples": excluded_make_samples,
        "excluded_pre_replay_facts": excluded_pre_replay,
        "replay_from": lower_bound.isoformat(),
        "input_checksum": _checksum(input_rows),
        "allocation_checksum": _checksum(allocation_rows_for_checksum),
    }
    batch_key = f"g{generation.id}:{generation.replay_version or _ALGORITHM_VERSION}"
    batch = (
        db.query(LedgerBuildBatch)
        .filter(
            LedgerBuildBatch.ledger_generation_id == generation.id,
            LedgerBuildBatch.stage == "reservation_replay",
            LedgerBuildBatch.batch_key == batch_key,
        )
        .one_or_none()
    )
    if batch is None:
        batch = LedgerBuildBatch(
            ledger_generation_id=generation.id,
            stage="reservation_replay",
            batch_key=batch_key,
            status="completed",
            algorithm_version=_ALGORITHM_VERSION,
            metrics=metrics,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(batch)
    else:
        batch.status = "completed"
        batch.metrics = metrics
        batch.completed_at = datetime.now(timezone.utc)
    db.flush()
    return {"ledger_generation_id": int(generation.id), "batch_id": int(batch.id), **metrics}
