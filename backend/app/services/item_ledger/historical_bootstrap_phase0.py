"""Phase-0 bootstrap helpers for historical Ledger restoration.

The helpers are read-write and scoped to a single historical BUILDING
generation:

* seed an opening balance (`seed_historical_opening_balance`)
* run a read-only Balance convergence gate (`evaluate_historical_balance_convergence`)

Both helpers are intentionally narrow and do not touch routers or lifecycle
publishing. They only mutate `LedgerGeneration.source_watermarks`/boundary links and
physical seed rows for bootstrap bootstraping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models

from .generation_bootstrap import (
    ALGORITHM_VERSION,
    _lineage_values,
)
from .physical import (
    LedgerKey,
    canonical_content_hash,
    canonical_decimal,
    guard_physical_batch_writer,
    seed_from_balance,
)
from .physical import EPS
from .physical_visibility import visible_sles_for_generation


class Phase0BootstrapError(RuntimeError):
    """A bounded bootstrap action cannot safely run against the current state."""


OPENING_BALANCE_KEY = "opening_balance"
CONVERGENCE_KEY = "balance_convergence"
OPENING_SOURCE = "historical-bootstrap-opening-seed"
OPENING_CONTENT_HASH_KEY = "content_hash"
OPENING_AT_KEY = "opening_at"
OPENING_BATCH_ID_KEY = "physical_import_batch_id"
PHYSICAL_REFRESH_KIND = "physical_refresh"


@dataclass(frozen=True)
class OpeningBalanceSeedResult:
    ledger_generation_id: int
    physical_import_batch_id: int
    opening_at: datetime
    content_hash: str
    created: bool
    entries_created: int


@dataclass(frozen=True)
class BalanceConvergenceDelta:
    item_id: int
    organization_ref: str
    warehouse_ref1c: str
    balance_qty: str
    ledger_qty: str
    delta_qty: str
    matched: bool


@dataclass(frozen=True)
class BalanceConvergenceResult:
    ledger_generation_id: int
    cutoff: str
    checked_at: str
    valid: bool
    content_hash: str
    compared: int
    mismatched: int
    matched: int
    terminal_batch_id: int
    deltas: tuple[BalanceConvergenceDelta, ...]


def _utc(value: datetime, field: str) -> datetime:
    if value is None:
        raise Phase0BootstrapError(f"{field} is required")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _key_from_any(raw: Any) -> LedgerKey:
    if isinstance(raw, LedgerKey):
        return raw
    if not isinstance(raw, (tuple, list)) or len(raw) != 4:
        raise Phase0BootstrapError("balance snapshot keys must be LedgerKey or 4-tuple")
    return LedgerKey(*raw)


def _normalize_opening_snapshot(
    snapshot: Mapping[LedgerKey | tuple[int, str, str, str], Any]
) -> list[tuple[LedgerKey, Decimal]]:
    pairs: list[tuple[LedgerKey, Decimal]] = []
    for raw_key, raw_qty in snapshot.items():
        key = _key_from_any(raw_key)
        qty = _dec(raw_qty)
        if abs(qty) <= EPS:
            continue
        pairs.append((key, qty))
    pairs.sort(key=lambda item: tuple(item[0]))
    return pairs


def _assert_historical_building_generation(db: Session, generation_id: int) -> models.LedgerGeneration:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise Phase0BootstrapError(f"LedgerGeneration {generation_id} not found")
    if str(generation.status) != "building":
        raise Phase0BootstrapError("seed requires BUILDING generation")
    if str(generation.algorithm_version or "") != ALGORITHM_VERSION:
        raise Phase0BootstrapError(
            "seed is only supported for historical bootstrap algorithm"
        )
    if generation.cutoff is None:
        raise Phase0BootstrapError("generation requires cutoff")
    return generation


def _assert_physical_refresh_building_generation(
    db: Session, generation_id: int
) -> models.LedgerGeneration:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise Phase0BootstrapError(f"LedgerGeneration {generation_id} not found")
    if str(generation.status or "") != "building":
        raise Phase0BootstrapError("requires BUILDING physical refresh generation")
    if generation.cutoff is None:
        raise Phase0BootstrapError("generation requires cutoff")
    if generation.physical_import_batch_id is None:
        raise Phase0BootstrapError("generation has no physical import terminal")
    watermarks = _generation_source_watermarks(generation)
    if watermarks.get("generation_kind") != PHYSICAL_REFRESH_KIND:
        raise Phase0BootstrapError("generation requires generation_kind=physical_refresh")
    return generation


def _generation_source_watermarks(generation: models.LedgerGeneration) -> dict[str, Any]:
    watermarks = dict(generation.source_watermarks or {})
    if not isinstance(watermarks, dict):
        raise Phase0BootstrapError("generation source_watermarks must be a mapping")
    return watermarks


def _assert_no_physical_import_checkpoints(db: Session, generation: models.LedgerGeneration) -> None:
    checkpoint_count = db.query(models.LedgerBuildBatch.id).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
        models.LedgerBuildBatch.stage == "physical_import",
    ).count()
    if checkpoint_count:
        raise Phase0BootstrapError("opening balance must be created before physical_import checkpoints")


def _assert_empty_physical_ledger(db: Session) -> None:
    prior_anchors = db.query(models.StockLedgerAnchor.id).count()
    prior_entries = db.query(models.StockLedgerEntry.id).count()
    if prior_anchors or prior_entries:
        raise Phase0BootstrapError(
            "historical opening requires an empty physical Ledger; "
            f"found anchors={prior_anchors}, entries={prior_entries}"
        )


def _assert_no_interleaving_import_batches(
    db: Session,
    generation: models.LedgerGeneration,
) -> None:
    terminal = db.query(func.max(models.PhysicalImportBatch.id)).scalar()
    if terminal is None:
        return
    boundary = db.get(models.PhysicalImportBatch, int(generation.physical_import_batch_id))
    if boundary is None:
        raise Phase0BootstrapError("generation has no physical import boundary")
    if terminal != int(boundary.id):
        raise Phase0BootstrapError(
            "physical import sequence is not at generation boundary"
        )


def _batch_key(generation: models.LedgerGeneration, content_hash: str) -> str:
    return f"historical-bootstrap-opening:g{int(generation.id)}:{content_hash}"


def seed_historical_opening_balance(
    db: Session,
    *,
    ledger_generation_id: int,
    balance_snapshot: Mapping[LedgerKey | tuple[int, str, str, str], Any],
) -> OpeningBalanceSeedResult:
    """Create one opening seed boundary and set it as generation terminal.

    Repeated calls with same snapshot are idempotent. Repeated calls with a
    different snapshot against the same generation are rejected as conflicts.
    """
    generation = _assert_historical_building_generation(db, ledger_generation_id)
    watermarks = _generation_source_watermarks(generation)
    historical_from, _replay_from = _lineage_values(generation)

    opening_at = _utc(historical_from, "opening_at")
    opening_period = opening_at.date()
    normalized = _normalize_opening_snapshot(balance_snapshot)
    content_hash = canonical_content_hash(
        [
            [list(key), canonical_decimal(qty)]
            for key, qty in normalized
        ]
    )

    existing_opening = watermarks.get(OPENING_BALANCE_KEY)
    if isinstance(existing_opening, dict):
        expected_hash = str(existing_opening.get(OPENING_CONTENT_HASH_KEY) or "")
        previous_batch_id = existing_opening.get(OPENING_BATCH_ID_KEY)
        if expected_hash != content_hash:
            raise Phase0BootstrapError(
                "opening balance snapshot conflicts with stored content_hash"
            )
        if previous_batch_id is None:
            raise Phase0BootstrapError(
                "opening balance metadata is missing physical_import_batch_id"
            )
        if int(previous_batch_id) != int(generation.physical_import_batch_id):
            raise Phase0BootstrapError(
                "opening balance batch does not match current terminal physical import boundary"
            )
        return OpeningBalanceSeedResult(
            ledger_generation_id=int(generation.id),
            physical_import_batch_id=int(previous_batch_id),
            opening_at=opening_at,
            content_hash=expected_hash,
            created=False,
            entries_created=0,
        )

    try:
        _assert_no_physical_import_checkpoints(db, generation)
        _assert_empty_physical_ledger(db)
        _assert_no_interleaving_import_batches(db, generation)

        opening_batch_key = _batch_key(generation, content_hash)
        guard_physical_batch_writer(db)
        opening_batch = models.PhysicalImportBatch(
            batch_key=opening_batch_key,
            status="completed",
            cutoff=opening_at,
            source_watermarks={
                "source": OPENING_SOURCE,
                OPENING_CONTENT_HASH_KEY: content_hash,
                "generation_id": int(generation.id),
                "opening_at": opening_at.isoformat(),
                "historical_from_exclusive": _utc(
                    historical_from, "historical_from_exclusive"
                ).isoformat(),
            },
            completed_at=datetime.now(timezone.utc),
        )
        db.add(opening_batch)
        db.flush()

        created_entries = seed_from_balance(
            db,
            {key: qty for key, qty in normalized},
            anchor_period=opening_period,
            posting_at=opening_at,
            import_batch=opening_batch,
            ledger_generation_id=generation.id,
        )

        existing_watermarks = _generation_source_watermarks(generation)
        generation.source_watermarks = {
            **existing_watermarks,
            OPENING_BALANCE_KEY: {
                OPENING_CONTENT_HASH_KEY: content_hash,
                "created_at": _utc(datetime.now(timezone.utc), "created_at").isoformat(),
                OPENING_AT_KEY: opening_at.isoformat(),
                OPENING_BATCH_ID_KEY: int(opening_batch.id),
            },
        }
        generation.physical_import_batch_id = int(opening_batch.id)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise Phase0BootstrapError(str(exc)) from exc

    return OpeningBalanceSeedResult(
        ledger_generation_id=int(generation.id),
        physical_import_batch_id=int(opening_batch.id),
        opening_at=opening_at,
        content_hash=content_hash,
        created=True,
        entries_created=len(created_entries),
    )


def _aggregate_sles_for_convergence(
    db: Session,
    generation: models.LedgerGeneration,
) -> dict[tuple[int, str, str], Decimal]:
    rows = visible_sles_for_generation(
        db,
        ledger_generation_id=int(generation.id),
    )
    grouped: dict[tuple[int, str, str], Decimal] = {}
    for row in rows:
        key = (
            int(row.item_id),
            str(row.organization_ref or ""),
            str(row.warehouse_ref1c or ""),
        )
        grouped[key] = grouped.get(key, Decimal("0")) + _dec(row.qty)
    return grouped


def _aggregate_snapshot(
    snapshot: Mapping[LedgerKey | tuple[int, str, str, str], Any],
) -> dict[tuple[int, str, str], Decimal]:
    grouped: dict[tuple[int, str, str], Decimal] = {}
    for raw_key, raw_qty in snapshot.items():
        key = _key_from_any(raw_key)
        agg = (int(key.item_id), str(key.organization_ref or ""), str(key.warehouse_ref1c or ""))
        grouped[agg] = grouped.get(agg, Decimal("0")) + _dec(raw_qty)
    return grouped


def _snapshot_convergence_content_hash(snapshot: Mapping[tuple[int, str, str], Decimal]) -> str:
    return canonical_content_hash(
        [
            [list(key), canonical_decimal(value)]
            for key, value in sorted(snapshot.items())
        ]
    )


def evaluate_historical_balance_convergence(
    db: Session,
    *,
    ledger_generation_id: int,
    balance_snapshot: Mapping[LedgerKey | tuple[int, str, str, str], Any],
    checked_at: datetime | None = None,
    eps: Decimal = EPS,
) -> BalanceConvergenceResult:
    """Compare Balance without adjustments and seal the diagnostic result."""
    generation = _assert_historical_building_generation(db, ledger_generation_id)
    if generation.physical_import_batch_id is None:
        raise Phase0BootstrapError("generation has no physical import terminal")
    _generation_source_watermarks(generation)  # raises if malformed
    if not isinstance(generation.source_watermarks.get(OPENING_BALANCE_KEY), dict):
        raise Phase0BootstrapError("generation is missing opening_balance metadata")

    normalized_snapshot = _aggregate_snapshot(balance_snapshot)
    content_hash = _snapshot_convergence_content_hash(normalized_snapshot)
    visible = _aggregate_sles_for_convergence(db, generation)

    deltas: list[BalanceConvergenceDelta] = []
    compared = 0
    matched = 0
    mismatched = 0
    keys = set(visible.keys()) | set(normalized_snapshot.keys())

    for item_id, organization_ref, warehouse_ref1c in sorted(keys):
        agg_key = (item_id, organization_ref, warehouse_ref1c)
        balance_qty = normalized_snapshot.get(agg_key, Decimal("0"))
        ledger_qty = visible.get(agg_key, Decimal("0"))
        delta = balance_qty - ledger_qty
        close = abs(delta) <= eps
        if close:
            matched += 1
        else:
            mismatched += 1
        compared += 1
        deltas.append(
            BalanceConvergenceDelta(
                item_id=item_id,
                organization_ref=organization_ref,
                warehouse_ref1c=warehouse_ref1c,
                balance_qty=str(balance_qty.normalize()),
                ledger_qty=str(ledger_qty.normalize()),
                delta_qty=str(delta.normalize()),
                matched=close,
            )
        )

    valid = mismatched == 0
    checked = _utc(checked_at or datetime.now(timezone.utc), "checked_at")
    cutoff = _utc(generation.cutoff, "generation cutoff")
    convergence = {
        "checked_at": checked.isoformat(),
        "valid": valid,
        "cutoff": cutoff.isoformat(),
        "physical_import_batch_id": int(generation.physical_import_batch_id),
        "content_hash": content_hash,
        "compared": compared,
        "matched": matched,
        "mismatched": mismatched,
    }

    watermarks = _generation_source_watermarks(generation)
    try:
        generation.source_watermarks = {
            **watermarks,
            CONVERGENCE_KEY: convergence,
        }
        db.commit()
    except Exception as exc:
        db.rollback()
        raise Phase0BootstrapError(str(exc)) from exc

    return BalanceConvergenceResult(
        ledger_generation_id=int(generation.id),
        cutoff=cutoff.isoformat(),
        checked_at=checked.isoformat(),
        valid=valid,
        content_hash=content_hash,
        compared=compared,
        matched=matched,
        mismatched=mismatched,
        terminal_batch_id=int(generation.physical_import_batch_id),
        deltas=tuple(deltas),
    )


def evaluate_physical_refresh_balance_convergence(
    db: Session,
    *,
    ledger_generation_id: int,
    balance_snapshot: Mapping[LedgerKey | tuple[int, str, str, str], Any],
    checked_at: datetime | None = None,
    eps: Decimal = EPS,
    commit: bool = False,
) -> BalanceConvergenceResult:
    """Compare Balance without adjustments and persist a deterministic diagnostic."""
    generation = _assert_physical_refresh_building_generation(db, ledger_generation_id)
    normalized_snapshot = _aggregate_snapshot(balance_snapshot)
    content_hash = _snapshot_convergence_content_hash(normalized_snapshot)
    visible = _aggregate_sles_for_convergence(db, generation)

    deltas: list[BalanceConvergenceDelta] = []
    compared = 0
    matched = 0
    mismatched = 0
    keys = set(visible.keys()) | set(normalized_snapshot.keys())

    for item_id, organization_ref, warehouse_ref1c in sorted(keys):
        agg_key = (item_id, organization_ref, warehouse_ref1c)
        balance_qty = normalized_snapshot.get(agg_key, Decimal("0"))
        ledger_qty = visible.get(agg_key, Decimal("0"))
        delta = balance_qty - ledger_qty
        close = abs(delta) <= eps
        if close:
            matched += 1
        else:
            mismatched += 1
        compared += 1
        deltas.append(
            BalanceConvergenceDelta(
                item_id=item_id,
                organization_ref=organization_ref,
                warehouse_ref1c=warehouse_ref1c,
                balance_qty=str(balance_qty.normalize()),
                ledger_qty=str(ledger_qty.normalize()),
                delta_qty=str(delta.normalize()),
                matched=close,
            )
        )

    valid = mismatched == 0
    checked = _utc(checked_at or datetime.now(timezone.utc), "checked_at")
    cutoff = _utc(generation.cutoff, "generation cutoff")
    convergence = {
        "checked_at": checked.isoformat(),
        "valid": valid,
        "cutoff": cutoff.isoformat(),
        "physical_import_batch_id": int(generation.physical_import_batch_id),
        "content_hash": content_hash,
        "compared": compared,
        "matched": matched,
        "mismatched": mismatched,
    }

    watermarks = _generation_source_watermarks(generation)
    generation.source_watermarks = {
        **watermarks,
        CONVERGENCE_KEY: convergence,
    }
    if commit:
        db.commit()

    return BalanceConvergenceResult(
        ledger_generation_id=int(generation.id),
        cutoff=cutoff.isoformat(),
        checked_at=checked.isoformat(),
        valid=valid,
        content_hash=content_hash,
        compared=compared,
        matched=matched,
        mismatched=mismatched,
        terminal_batch_id=int(generation.physical_import_batch_id),
        deltas=tuple(deltas),
    )
