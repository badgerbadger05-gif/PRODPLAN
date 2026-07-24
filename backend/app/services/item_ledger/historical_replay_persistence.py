"""Generation-scoped persistence adapter for historical Item Ledger replay."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    LedgerBuildBatch,
    LedgerGeneration,
    MrpExecutionAllocation,
    MrpRequirementBucket,
    ReservationEntry,
    ReservationEvent,
    StockLedgerEntry,
)
from app import models
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C

from .historical_replay_core import Fact, Reserve, allocate_historical_facts
from .physical_visibility import visible_sles_for_generation
from .reconcile import contour_warehouse_refs


_ALGORITHM_VERSION = "historical-replay-persistence/1"
_SAFE_REALIZATION_KINDS = frozenset({"assembly_in", "assembly_out", "writeoff"})
_IGNORED_FACT_KINDS = frozenset({"receipt", "expense"})
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
    if mode_key == "consume":
        return max(_decimal(bucket.gross_qty), Decimal("0"))
    raise ValueError(f"unsupported realization mode for bucket capacity: {mode}")


def _checksum(rows: list[dict[str, Any]]) -> str:
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fact_mode(row: StockLedgerEntry) -> str:
    return "make" if str(row.movement_kind or "") == "assembly_in" else "consume"


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


def _legacy_unphased_requirement_ids(
    db: Session, generation_id: int
) -> set[int]:
    batch = (
        db.query(LedgerBuildBatch)
        .filter(
            LedgerBuildBatch.ledger_generation_id == generation_id,
            LedgerBuildBatch.stage == "reservation_materialize",
            LedgerBuildBatch.status == "completed",
        )
        .one_or_none()
    )
    if batch is None or not isinstance(batch.metrics, dict):
        return set()
    legacy_net_phasing_requirement_ids = batch.metrics.get(
        "legacy_net_phasing_requirement_ids"
    )
    if legacy_net_phasing_requirement_ids is None:
        return set()
    if not isinstance(legacy_net_phasing_requirement_ids, (list, tuple)):
        raise ValueError("legacy reservation batch has malformed legacy_net_phasing_requirement_ids")
    ids: set[int] = set()
    for value in legacy_net_phasing_requirement_ids:
        try:
            ids.add(int(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "legacy reservation batch legacy_net_phasing_requirement_ids must contain integer ids"
            ) from exc
    return ids


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
        models.SyncLink.source_doctype.in_(("manufacture", "material_issue")),
    ).all()
    order_ids: set[int] = set()
    for link in links:
        if link.source_doctype == "manufacture":
            source = db.get(models.ProductionManufacture, int(link.source_id))
        else:
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

    entries = (
        db.query(ReservationEntry)
        .filter(
            ReservationEntry.ledger_generation_id == generation.id,
            ReservationEntry.lifecycle_status == "active",
        )
        .order_by(ReservationEntry.id.asc())
        .all()
    )
    requirement_ids = [int(row.requirement_id) for row in entries]
    legacy_unphased_requirement_ids = _legacy_unphased_requirement_ids(
        db, int(generation.id)
    )
    legacy_unphased_requirement_ids = {
        requirement_id
        for requirement_id in legacy_unphased_requirement_ids
        if requirement_id
        in {
            int(entry.requirement_id)
            for entry in entries
            if str(entry.realization_mode).lower() == "make"
        }
    }
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
            reserved_qty=_decimal(row.reserved_qty),
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
    identity_by_core_id: dict[str, tuple[int | None, str | None]] = {}
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
        identity_by_core_id[core_id] = (requirement_id, order_ref)

    result = allocate_historical_facts(facts, reserves)
    cycle_id = f"historical-replay:g{generation.id}"
    inserted_events = 0
    inserted_allocations = 0
    bucket_used: dict[tuple[int, str], Decimal] = {}
    allocation_rows_for_checksum: list[dict[str, Any]] = []
    for allocation in result.allocations:
        entry = entry_by_core_id[allocation.reserve_id]
        sle = sle_by_core_id[allocation.fact_id]
        idempotency_key = f"hist:g{generation.id}:sle{sle.id}:r{entry.id}"
        mode = _fact_mode(sle)
        event = (
            db.query(ReservationEvent)
            .filter(
                ReservationEvent.ledger_generation_id == generation.id,
                ReservationEvent.idempotency_key == idempotency_key,
            )
            .one_or_none()
        )
        if event is None:
            event = ReservationEvent(
                ledger_generation_id=generation.id,
                reservation_id=entry.id,
                item_id=entry.item_id,
                characteristic_ref=entry.characteristic_ref,
                organization_ref=entry.organization_ref,
                planning_stock_pool=entry.planning_stock_pool,
                event_kind="realize",
                reserved_delta=Decimal("0"),
                realized_delta=allocation.qty,
                sle_id=sle.id,
                fact_ref=str(sle.recorder_ref or f"sle:{sle.id}"),
                fact_line_ref=str(sle.line_no or ""),
                match_rule="fifo" if allocation.match_rule == "fifo" else "pegged",
                cycle_id=cycle_id,
                idempotency_key=idempotency_key,
                event_at=sle.posting_at,
            )
            db.add(event)
            inserted_events += 1

        fact_ref = str(sle.recorder_ref or f"sle:{sle.id}")
        fact_line_ref = str(sle.line_no or "")
        resolved_requirement_id, _resolved_order_ref = identity_by_core_id[allocation.fact_id]
        fact_type = (
            (
                "linked_production"
                if resolved_requirement_id is not None
                else "unlinked_production"
            )
            if _fact_mode(sle) == "make"
            else "component_consumption"
        )
        buckets = buckets_by_requirement.get(int(entry.requirement_id), [])
        existing_slices = (
            db.query(MrpExecutionAllocation)
            .filter(
                MrpExecutionAllocation.ledger_generation_id == generation.id,
                MrpExecutionAllocation.requirement_id == entry.requirement_id,
                MrpExecutionAllocation.fact_type == fact_type,
                MrpExecutionAllocation.fact_ref == fact_ref,
                MrpExecutionAllocation.fact_line_ref == fact_line_ref,
                MrpExecutionAllocation.allocation_kind == "execution",
            )
            .all()
        )
        if existing_slices:
            if sum(
                (_decimal(row.allocated_qty) for row in existing_slices),
                Decimal("0"),
            ) != _decimal(allocation.qty):
                raise ValueError(
                    f"existing allocation slices disagree for fact {fact_ref}/{fact_line_ref}"
                )
            for existing in existing_slices:
                if existing.bucket_id is not None:
                    used_key = (int(existing.bucket_id), mode)
                    bucket_used[used_key] = (
                        bucket_used.get(used_key, Decimal("0"))
                        + _decimal(existing.allocated_qty)
                    )
            slices = []
        else:
            slices: list[tuple[int | None, Decimal]] = []
            left = _decimal(allocation.qty)
            if buckets:
                if (
                    mode == "make"
                    and int(entry.requirement_id) in legacy_unphased_requirement_ids
                ):
                    slices = [(None, left)]
                else:
                    for bucket in buckets:
                        capacity = bucket_capacity_for_mode(bucket, mode)
                        used_key = (int(bucket.id), mode)
                        take = min(
                            left,
                            max(
                                capacity - bucket_used.get(used_key, Decimal("0")),
                                Decimal("0"),
                            ),
                        )
                        if take > 0:
                            slices.append((int(bucket.id), take))
                            bucket_used[used_key] = (
                                bucket_used.get(used_key, Decimal("0")) + take
                            )
                            left -= take
                        if left <= 0:
                            break
                    if left > 0:
                        raise ValueError(
                            f"requirement {entry.requirement_id} bucket capacity is below realization"
                        )
            else:
                slices.append((None, left))
        for bucket_id, slice_qty in slices:
            execution = (
                db.query(MrpExecutionAllocation)
                .filter(
                    MrpExecutionAllocation.ledger_generation_id == generation.id,
                    MrpExecutionAllocation.requirement_id == entry.requirement_id,
                    MrpExecutionAllocation.bucket_id == bucket_id,
                    MrpExecutionAllocation.fact_type == fact_type,
                    MrpExecutionAllocation.fact_ref == fact_ref,
                    MrpExecutionAllocation.fact_line_ref == fact_line_ref,
                    MrpExecutionAllocation.allocation_kind == "execution",
                )
                .one_or_none()
            )
            if execution is None:
                db.add(MrpExecutionAllocation(
                    ledger_generation_id=generation.id,
                    cycle_id=cycle_id,
                    requirement_id=entry.requirement_id,
                    bucket_id=bucket_id,
                    fact_type=fact_type,
                    allocation_kind="execution",
                    fact_ref=fact_ref,
                    fact_line_ref=fact_line_ref,
                    fact_date=sle.posting_at,
                    allocated_qty=slice_qty,
                ))
                inserted_allocations += 1
        allocation_rows_for_checksum.append({
            "sle_id": int(sle.id),
            "reservation_id": int(entry.id),
            "requirement_id": int(entry.requirement_id),
            "qty": str(allocation.qty),
            "rule": allocation.match_rule,
        })

    db.flush()
    for entry in entries:
        realized = (
            db.query(func.coalesce(func.sum(ReservationEvent.realized_delta), 0))
            .filter(
                ReservationEvent.ledger_generation_id == generation.id,
                ReservationEvent.reservation_id == entry.id,
            )
            .scalar()
        )
        entry.realized_qty = _decimal(realized)

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
        "execution_allocations_inserted": inserted_allocations,
        "fact_qty": str(result.fact_qty),
        "allocated_qty": str(result.allocated_qty),
        "unplanned_qty": str(result.unplanned_qty),
        "unplanned_facts": len(result.unplanned),
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
