"""Keep the opening balance honest when 1C changes history behind the anchor.

The physical ledger models detail only after the anchor T0: movements at or
below it are dropped as pre-anchor because the opening seed is supposed to
contain them already.  That premise breaks when a document dated before T0 is
posted *after* the seed snapshot was taken — 1C's balance then includes it, the
seed does not, and no amount of recorder discovery can help: the movement is
pre-anchor by construction and the seed predates the document.

Re-seeding is not the answer either; it would have to happen after every such
document.  Instead this stage re-asks 1C what the balance at T0 is *today*,
compares it with what the ledger holds at that boundary, and writes the
difference as one explicit adjustment movement per key, dated at T0.  The
ledger's history stays append-only, its present balance matches 1C again, and
the next run recomputes a zero difference — so the correction does not repeat.

Adjustments are derived from 1C's own answer for the T0 prefix, never from a
convergence mismatch.  That distinction matters: a plug taken from our own
disagreement would silently absorb exactly the post-anchor import bugs the
convergence check exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app import models

from .physical import (
    LedgerKey,
    _dec,
    canonical_content_hash,
    canonical_decimal,
    guard_physical_batch_writer,
    rebuild_running_balance,
)
from .physical_visibility import visible_sle_query


ALGORITHM_VERSION = "ledger-opening-balance-reconcile/1"
CHECKPOINT_KEY_PREFIX = "opening-balance-reconcile"
CHECKPOINT_VERSION = "1"
STAGE = "physical_import"
ADJUSTMENT_SOURCE = "opening_adjustment"
ADJUSTMENT_RECORDER_TYPE = "opening_adjustment"
OPENING_AT_KEY = "opening_at"
WATERMARK_KEY = "opening_balance_reconcile"
EPS = Decimal("0.001")
# A few documents backdated behind the anchor are routine.  A mass shift is a
# different event — a re-posted period, a changed organization, a bad snapshot —
# and must not rewrite the opening balance unattended.
MAX_ADJUSTED_KEYS = 500
# Adjustment rows are ordinary ledger facts, so their recorder identity has to
# fit the same column every pulled recorder uses.
RECORDER_REF_LIMIT = int(
    models.StockLedgerEntry.__table__.c.recorder_ref.type.length or 64
)

AggregateKey = tuple[int, str, str]


class OpeningBalanceReconcileError(RuntimeError):
    """The opening balance cannot be reconciled safely against 1C."""


@dataclass(frozen=True)
class OpeningBalanceAdjustment:
    item_id: int
    organization_ref: str
    warehouse_ref1c: str
    ledger_qty: str
    balance_qty: str
    delta_qty: str


@dataclass(frozen=True)
class OpeningBalanceReconcileResult:
    ledger_generation_id: int
    opening_at: datetime
    compared: int
    adjusted_keys: int
    net_delta: str
    content_hash: str
    physical_import_batch_id: int | None
    checkpoint_id: int | None
    created: bool
    adjustments: tuple[OpeningBalanceAdjustment, ...]


def _utc(value: Any, field: str) -> datetime:
    if value is None:
        raise OpeningBalanceReconcileError(f"{field} is missing")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OpeningBalanceReconcileError(f"{field} is not an ISO datetime") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def opening_boundary(db: Session) -> tuple[models.PhysicalImportBatch, datetime] | None:
    """The opening-balance seed boundary and the instant it describes.

    Recognised by the ``opening_at`` watermark rather than by ``source``:
    ensure_physical_import_batch rewrites ``source`` to the seed ingest source,
    so only ``opening_at`` reliably marks this boundary.
    """
    batch = (
        db.query(models.PhysicalImportBatch)
        .filter(
            models.PhysicalImportBatch.source_watermarks[OPENING_AT_KEY]
            .as_string()
            .isnot(None)
        )
        .order_by(models.PhysicalImportBatch.id.asc())
        .first()
    )
    if batch is None:
        return None
    raw = dict(batch.source_watermarks or {}).get(OPENING_AT_KEY)
    if raw is None:
        return None
    return batch, _utc(raw, OPENING_AT_KEY)


def _aggregate_key(raw: Any) -> AggregateKey:
    key = raw if isinstance(raw, LedgerKey) else LedgerKey(*raw)
    return int(key.item_id), str(key.organization_ref or ""), str(key.warehouse_ref1c or "")


def _aggregate_snapshot(snapshot: Mapping[Any, Any]) -> dict[AggregateKey, Decimal]:
    grouped: dict[AggregateKey, Decimal] = {}
    for raw_key, raw_qty in snapshot.items():
        key = _aggregate_key(raw_key)
        grouped[key] = grouped.get(key, Decimal("0")) + _dec(raw_qty)
    return grouped


def _ledger_prefix(
    db: Session,
    *,
    physical_import_batch_id: int,
    opening_at: datetime,
) -> dict[AggregateKey, Decimal]:
    """What the ledger holds at the opening boundary: seed plus past adjustments."""
    grouped: dict[AggregateKey, Decimal] = {}
    rows = visible_sle_query(
        db,
        physical_import_batch_id=int(physical_import_batch_id),
        cutoff=opening_at,
    ).all()
    for row in rows:
        key = (
            int(row.item_id),
            str(row.organization_ref or ""),
            str(row.warehouse_ref1c or ""),
        )
        grouped[key] = grouped.get(key, Decimal("0")) + _dec(row.qty)
    return grouped


def _checkpoint_key(generation_id: int) -> str:
    return f"{CHECKPOINT_KEY_PREFIX}:{int(generation_id)}:{CHECKPOINT_VERSION}"


def _require_building_generation(db: Session, generation_id: int) -> models.LedgerGeneration:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise OpeningBalanceReconcileError(f"generation {generation_id} does not exist")
    if str(generation.status) != "building":
        raise OpeningBalanceReconcileError(
            f"generation {generation.id} is {generation.status}; BUILDING required"
        )
    if int(generation.physical_import_batch_id or 0) <= 0:
        raise OpeningBalanceReconcileError("generation has no physical import boundary")
    return generation


def _adjustment_recorder_ref(opening_at: datetime, key: AggregateKey) -> str:
    """Deterministic recorder identity for one adjusted key.

    The identity hash is truncated to whatever ``recorder_ref`` can still hold
    after the readable prefix, so the column width stays the single source of
    truth rather than a hand-counted literal.
    """
    identity = canonical_content_hash({
        "opening_at": opening_at.isoformat(),
        "item_id": key[0],
        "organization_ref": key[1],
        "warehouse_ref1c": key[2],
    })
    prefix = f"{ADJUSTMENT_SOURCE}:{opening_at.date().isoformat()}:"
    room = RECORDER_REF_LIMIT - len(prefix)
    if room < 16:
        raise OpeningBalanceReconcileError(
            f"recorder_ref prefix {prefix!r} leaves only {room} characters for the "
            "identity hash; the adjustment identity would not be collision-safe"
        )
    return f"{prefix}{identity[:room]}"


def reconcile_opening_balance(
    db: Session,
    *,
    ledger_generation_id: int,
    opening_snapshot: Mapping[Any, Any],
    eps: Decimal = EPS,
    max_adjusted_keys: int = MAX_ADJUSTED_KEYS,
) -> OpeningBalanceReconcileResult:
    """Align the ledger's T0 prefix with 1C's present answer for that instant.

    ``opening_snapshot`` is the 1C Balance as of the anchor, fetched now, in the
    same shape the convergence check consumes.  Returns ``created=False`` and
    writes nothing when the two already agree, which is the steady state.
    """
    generation = _require_building_generation(db, ledger_generation_id)
    boundary = opening_boundary(db)
    if boundary is None:
        raise OpeningBalanceReconcileError(
            "no opening-balance boundary exists; nothing to reconcile against"
        )
    _opening_batch, opening_at = boundary

    existing = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
        models.LedgerBuildBatch.stage == STAGE,
        models.LedgerBuildBatch.batch_key == _checkpoint_key(int(generation.id)),
    ).one_or_none()
    if existing is not None:
        if str(existing.status) != "completed":
            raise OpeningBalanceReconcileError(
                "existing opening-balance checkpoint is not completed"
            )
        metrics = dict(existing.metrics or {})
        return OpeningBalanceReconcileResult(
            ledger_generation_id=int(generation.id),
            opening_at=opening_at,
            compared=int(metrics.get("compared") or 0),
            adjusted_keys=int(metrics.get("adjusted_keys") or 0),
            net_delta=str(metrics.get("net_delta") or "0"),
            content_hash=str(metrics.get("content_hash") or ""),
            physical_import_batch_id=metrics.get("physical_import_batch_id"),
            checkpoint_id=int(existing.id),
            created=False,
            adjustments=(),
        )

    balance = _aggregate_snapshot(opening_snapshot)
    ledger = _ledger_prefix(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        opening_at=opening_at,
    )

    adjustments: list[OpeningBalanceAdjustment] = []
    for key in sorted(set(balance) | set(ledger)):
        balance_qty = balance.get(key, Decimal("0"))
        ledger_qty = ledger.get(key, Decimal("0"))
        delta = balance_qty - ledger_qty
        if abs(delta) <= eps:
            continue
        adjustments.append(OpeningBalanceAdjustment(
            item_id=key[0],
            organization_ref=key[1],
            warehouse_ref1c=key[2],
            ledger_qty=canonical_decimal(ledger_qty),
            balance_qty=canonical_decimal(balance_qty),
            delta_qty=canonical_decimal(delta),
        ))

    compared = len(set(balance) | set(ledger))
    content_hash = canonical_content_hash([
        [row.item_id, row.organization_ref, row.warehouse_ref1c, row.delta_qty]
        for row in adjustments
    ])
    net_delta = canonical_decimal(
        sum((Decimal(row.delta_qty) for row in adjustments), Decimal("0"))
    )

    if not adjustments:
        return OpeningBalanceReconcileResult(
            ledger_generation_id=int(generation.id),
            opening_at=opening_at,
            compared=compared,
            adjusted_keys=0,
            net_delta=net_delta,
            content_hash=content_hash,
            physical_import_batch_id=None,
            checkpoint_id=None,
            created=False,
            adjustments=(),
        )

    if len(adjustments) > int(max_adjusted_keys):
        raise OpeningBalanceReconcileError(
            f"opening balance moved on {len(adjustments)} keys, above the "
            f"{max_adjusted_keys} key safety limit; refusing to rewrite it unattended"
        )

    guard_physical_batch_writer(db)
    batch = models.PhysicalImportBatch(
        batch_key=f"{CHECKPOINT_KEY_PREFIX}:{int(generation.id)}:{content_hash[:40]}",
        status="building",
        cutoff=opening_at,
        source_watermarks={
            "source": ADJUSTMENT_SOURCE,
            "generation_id": int(generation.id),
            OPENING_AT_KEY: opening_at.isoformat(),
            "content_hash": content_hash,
            "adjusted_keys": len(adjustments),
            "previous_import_batch_id": int(generation.physical_import_batch_id),
        },
    )
    db.add(batch)
    db.flush()

    for row in adjustments:
        key = LedgerKey(int(row.item_id), "", row.organization_ref, row.warehouse_ref1c)
        qty = Decimal(row.delta_qty)
        recorder_ref = _adjustment_recorder_ref(opening_at, (key.item_id, key.organization_ref, key.warehouse_ref1c))
        entry = models.StockLedgerEntry(
            ingest_batch_id=int(batch.id),
            source_content_hash=canonical_content_hash({
                "recorder_type": ADJUSTMENT_RECORDER_TYPE,
                "recorder_ref": recorder_ref,
                "line_no": "0",
                "qty": canonical_decimal(qty),
                "posting_at": opening_at.isoformat(),
            }),
            item_id=key.item_id,
            characteristic_ref="",
            organization_ref=key.organization_ref,
            warehouse_ref1c=key.warehouse_ref1c,
            qty=qty,
            posting_at=opening_at,
            record_type="Receipt" if qty > 0 else "Expense",
            movement_kind=ADJUSTMENT_SOURCE,
            recorder_type=ADJUSTMENT_RECORDER_TYPE,
            recorder_ref=recorder_ref,
            line_no="0",
            ingest_source=ADJUSTMENT_SOURCE,
        )
        db.add(entry)
        db.flush()

        # A key 1C knows but the seed never covered has no anchor, so nothing
        # would stop a later pull from re-importing its pre-T0 history on top of
        # this adjustment.  Give it the same guard every seeded key has.
        anchor_exists = db.query(models.StockLedgerAnchor.id).filter(
            models.StockLedgerAnchor.item_id == key.item_id,
            models.StockLedgerAnchor.characteristic_ref == "",
            models.StockLedgerAnchor.organization_ref == key.organization_ref,
            models.StockLedgerAnchor.warehouse_ref1c == key.warehouse_ref1c,
            models.StockLedgerAnchor.anchor_period == opening_at.date(),
        ).first()
        if anchor_exists is None:
            db.add(models.StockLedgerAnchor(
                ingest_batch_id=int(batch.id),
                item_id=key.item_id,
                characteristic_ref="",
                organization_ref=key.organization_ref,
                warehouse_ref1c=key.warehouse_ref1c,
                anchor_period=opening_at.date(),
                anchor_at=opening_at,
                balance_qty=Decimal(row.balance_qty),
                source="balance_seed",
                entry_id=int(entry.id),
            ))
        rebuild_running_balance(db, key, ledger_generation_id=int(generation.id))

    metrics = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "opening_at": opening_at.isoformat(),
        "compared": compared,
        "adjusted_keys": len(adjustments),
        "net_delta": net_delta,
        "content_hash": content_hash,
        "physical_import_batch_id": int(batch.id),
        "previous_import_batch_id": int(generation.physical_import_batch_id),
        "adjustments": [
            {
                "item_id": row.item_id,
                "organization_ref": row.organization_ref,
                "warehouse_ref1c": row.warehouse_ref1c,
                "ledger_qty": row.ledger_qty,
                "balance_qty": row.balance_qty,
                "delta_qty": row.delta_qty,
            }
            for row in adjustments
        ],
    }
    batch.status = "completed"
    batch.completed_at = datetime.now(timezone.utc)
    batch.source_watermarks = {**dict(batch.source_watermarks or {}), **metrics}

    checkpoint = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage=STAGE,
        batch_key=_checkpoint_key(int(generation.id)),
        status="completed",
        algorithm_version=ALGORITHM_VERSION,
        metrics=metrics,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(checkpoint)
    db.flush()

    generation.physical_import_batch_id = int(batch.id)
    generation.source_watermarks = {
        **dict(generation.source_watermarks or {}),
        WATERMARK_KEY: {
            "version": CHECKPOINT_VERSION,
            "opening_at": opening_at.isoformat(),
            "adjusted_keys": len(adjustments),
            "net_delta": net_delta,
            "content_hash": content_hash,
            "physical_import_batch_id": int(batch.id),
        },
    }
    db.flush()

    return OpeningBalanceReconcileResult(
        ledger_generation_id=int(generation.id),
        opening_at=opening_at,
        compared=compared,
        adjusted_keys=len(adjustments),
        net_delta=net_delta,
        content_hash=content_hash,
        physical_import_batch_id=int(batch.id),
        checkpoint_id=int(checkpoint.id),
        created=True,
        adjustments=tuple(adjustments),
    )
