"""Ledger-1 (physical movements) math + seeding — , , .

Pure helper (`fold_running_balance`) plus two Session-taking writers that only
touch the new ledger tables (`rebuild_running_balance`, `seed_from_balance`).
No OData, no writes into any pre-existing table (INV-1way / INV-no-write).
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, List, Mapping, NamedTuple, Optional, Tuple, Union

from sqlalchemy import and_, text
from sqlalchemy.orm import Session

from app import models

EPS = Decimal("1e-9")
# One global lock space for every writer of the id-prefix physical history.
# A refresh holds the session-level form across its checkpoint commits; an
# individual recorder pull takes the transaction-level form.
PHYSICAL_SEQUENCE_LOCK_KEY = 0x706879732D726566  # signed bigint: "phys-ref"
_physical_sequence_lock_owned: ContextVar[bool] = ContextVar(
    "physical_sequence_lock_owned", default=False
)


@contextmanager
def physical_sequence_lock_context():
    """Mark the current physical-refresh call stack as lock-owner only."""
    token = _physical_sequence_lock_owned.set(True)
    try:
        yield
    finally:
        _physical_sequence_lock_owned.reset(token)


def guard_physical_batch_writer(session: Session) -> None:
    """Serialize every PhysicalImportBatch writer on PostgreSQL.

    The lifecycle sets a ContextVar while holding the session-level lock on its
    dedicated connection; internal writers then safely skip this transaction
    lock. All standalone writers retain the default advisory guard.
    """
    if _physical_sequence_lock_owned.get():
        return
    try:
        dialect = session.get_bind().dialect.name
    except Exception:
        dialect = ""
    if dialect == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": PHYSICAL_SEQUENCE_LOCK_KEY},
        )

Number = Union[int, float, Decimal, str]


def _dec(value: Number) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class LedgerKey(NamedTuple):
    """Physical key of ledger-1 (): the axis Balance-сверка can see."""

    item_id: int
    characteristic_ref: str = ""
    organization_ref: str = ""
    warehouse_ref1c: str = ""


def canonical_content_hash(value: Any) -> str:
    """SHA-256 of a stable JSON representation used for physical fact identity."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_decimal(value: Number) -> str:
    """Stable non-exponent decimal text (5, 5.0 and Decimal('5.000') match)."""
    normalized = _dec(value).normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def ensure_physical_import_batch(
    session: Session,
    *,
    batch_key: str,
    cutoff: Optional[datetime],
    source_watermarks: Mapping[str, Any],
    batch: Optional[models.PhysicalImportBatch] = None,
) -> models.PhysicalImportBatch:
    """Return an explicit physical import boundary, creating it idempotently."""
    guard_physical_batch_writer(session)
    if batch is not None:
        retained = dict(batch.source_watermarks or {})
        retained.update(dict(source_watermarks))
        batch.source_watermarks = retained
        if batch.cutoff is None:
            batch.cutoff = cutoff
        session.add(batch)
        session.flush()
        return batch
    existing = (
        session.query(models.PhysicalImportBatch)
        .filter(models.PhysicalImportBatch.batch_key == batch_key)
        .one_or_none()
    )
    if existing is not None:
        return existing
    created = models.PhysicalImportBatch(
        batch_key=batch_key,
        status="completed",
        cutoff=cutoff,
        source_watermarks=dict(source_watermarks),
        completed_at=datetime.now(),
    )
    session.add(created)
    session.flush()
    return created


def _lock_ledger_key(session: Session, ledger_key: LedgerKey) -> None:
    """Serialize projection rewrites for a physical key on PostgreSQL."""
    try:
        dialect = session.get_bind().dialect.name
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return
    digest = hashlib.sha1(
        "\x1f".join(map(str, LedgerKey(*ledger_key))).encode("utf-8")
    ).digest()[:8]
    session.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": int.from_bytes(digest, "big", signed=True)},
    )


def fold_running_balance(
    qtys: Iterable[Number],
    start: Number = Decimal("0"),
) -> Tuple[List[Decimal], Decimal]:
    """PURE fold: signed movement quantities → (running qty_after list, final).

    on_hand = Σ qty = final = last qty_after ( R-A / ). No clamping
    of negatives — a negative running balance is a real reconcile signal (a).
    """
    running = _dec(start)
    qty_after: List[Decimal] = []
    for q in qtys:
        running = running + _dec(q)
        qty_after.append(running)
    return qty_after, running


def _key_of(entry: models.StockLedgerEntry) -> LedgerKey:
    return LedgerKey(
        entry.item_id,
        entry.characteristic_ref or "",
        entry.organization_ref or "",
        entry.warehouse_ref1c or "",
    )


def rebuild_running_balance(
    session: Session,
    ledger_key: LedgerKey,
    from_posting_at: Optional[datetime] = None,
    *,
    ledger_generation_id: Optional[int] = None,
) -> Decimal:
    """Recompute qty_after forward for one ledger key and refold stock_bin.on_hand.

    Rows are ordered by (posting_at, id) — insertion order within a timestamp.
    ``from_posting_at`` limits the rewrite window: the opening balance is the
    qty_after of the last row strictly before it ( narrow rebuild).
    Returns the final on_hand. Writes only stock_ledger_entry.qty_after and
    stock_bin (both ledger-1 tables).
    """
    ledger_key = LedgerKey(*ledger_key)
    _lock_ledger_key(session, ledger_key)
    base_filter = and_(
        models.StockLedgerEntry.item_id == ledger_key.item_id,
        models.StockLedgerEntry.characteristic_ref == ledger_key.characteristic_ref,
        models.StockLedgerEntry.organization_ref == ledger_key.organization_ref,
        models.StockLedgerEntry.warehouse_ref1c == ledger_key.warehouse_ref1c,
        models.StockLedgerEntry.active.is_(True),
    )

    ordered = (
        session.query(models.StockLedgerEntry)
        .filter(base_filter)
        .order_by(models.StockLedgerEntry.posting_at.asc(), models.StockLedgerEntry.id.asc())
        .all()
    )

    start = Decimal("0")
    to_rewrite = ordered
    if from_posting_at is not None:
        prior = [e for e in ordered if e.posting_at is not None and e.posting_at < from_posting_at]
        if prior:
            start = _dec(prior[-1].qty_after)
        to_rewrite = [
            e for e in ordered if e.posting_at is None or e.posting_at >= from_posting_at
        ]

    running = start
    last_entry_id = ordered[-1].id if ordered else None
    for entry in to_rewrite:
        running = running + _dec(entry.qty)
        entry.qty_after = running

    # on_hand is the fold over the whole key (= last qty_after).
    on_hand = ordered[-1].qty_after if ordered else Decimal("0")
    on_hand = _dec(on_hand)

    if ledger_generation_id is not None:
        bin_row = (
            session.query(models.StockBin)
            .filter(
                models.StockBin.ledger_generation_id == int(ledger_generation_id),
                models.StockBin.item_id == ledger_key.item_id,
                models.StockBin.characteristic_ref == ledger_key.characteristic_ref,
                models.StockBin.organization_ref == ledger_key.organization_ref,
                models.StockBin.warehouse_ref1c == ledger_key.warehouse_ref1c,
            )
            .one_or_none()
        )
        if bin_row is None:
            bin_row = models.StockBin(
                ledger_generation_id=int(ledger_generation_id),
                item_id=ledger_key.item_id,
                characteristic_ref=ledger_key.characteristic_ref,
                organization_ref=ledger_key.organization_ref,
                warehouse_ref1c=ledger_key.warehouse_ref1c,
            )
            session.add(bin_row)
        bin_row.on_hand = on_hand
        bin_row.last_entry_id = last_entry_id
    session.flush()
    return on_hand


# Recorder identity of the opening seed. Named so consumers that must never
# re-pull synthetic rows from 1C can reference it instead of the literal.
SEED_RECORDER_TYPE = "seed"


def seed_from_balance(
    session: Session,
    balance_snapshot: Mapping[LedgerKey, Number],
    anchor_period: date,
    posting_at: Optional[datetime] = None,
    ingest_source: str = "seed",
    *,
    import_batch: Optional[models.PhysicalImportBatch] = None,
    ledger_generation_id: Optional[int] = None,
) -> List[models.StockLedgerEntry]:
    """Seed ledger-1 from a Balance dict {ledger_key: qty} ( seed / ).

    Writes one seed SLE per key (movement_kind='seed', qty_after == qty), builds
    the stock_bin, and records a stock_ledger_anchor for the period. The ACTUAL
    OData Balance pull is  — here only the seeding logic given a dict is
    exercised (no OData). Idempotent per (period, key) via the recorder unique
    key and the anchor unique key; re-seeding a present key is skipped.
    """
    posting_at = posting_at or datetime.combine(anchor_period, datetime.min.time())
    created: List[models.StockLedgerEntry] = []
    snapshot_hash = canonical_content_hash(
        [
            [list(LedgerKey(*key)), canonical_decimal(qty)]
            for key, qty in sorted(balance_snapshot.items(), key=lambda pair: tuple(pair[0]))
        ]
    )
    import_batch = ensure_physical_import_batch(
        session,
        batch_key=f"seed:{anchor_period.isoformat()}:{snapshot_hash}",
        cutoff=posting_at,
        source_watermarks={
            "source": ingest_source,
            "anchor_period": anchor_period.isoformat(),
            "content_hash": snapshot_hash,
        },
        batch=import_batch,
    )

    for raw_key, raw_qty in balance_snapshot.items():
        key = LedgerKey(*raw_key)
        qty = _dec(raw_qty)
        recorder_identity_hash = canonical_content_hash({
            "anchor_period": anchor_period.isoformat(),
            "item_id": key.item_id,
            "characteristic_ref": key.characteristic_ref,
            "organization_ref": key.organization_ref,
            "warehouse_ref1c": key.warehouse_ref1c,
        })
        recorder_ref = (
            f"seed:{anchor_period.isoformat()}:{recorder_identity_hash[:40]}"
        )

        existing_anchor = (
            session.query(models.StockLedgerAnchor)
            .filter(
                models.StockLedgerAnchor.item_id == key.item_id,
                models.StockLedgerAnchor.characteristic_ref == key.characteristic_ref,
                models.StockLedgerAnchor.organization_ref == key.organization_ref,
                models.StockLedgerAnchor.warehouse_ref1c == key.warehouse_ref1c,
                models.StockLedgerAnchor.anchor_period == anchor_period,
            )
            .one_or_none()
        )
        if existing_anchor is not None:
            continue  # already seeded for this period/key — idempotent skip.

        entry = models.StockLedgerEntry(
            ingest_batch_id=import_batch.id,
            source_content_hash=canonical_content_hash(
                {
                    "recorder_type": "seed",
                    "recorder_ref": recorder_ref,
                    "line_no": "0",
                    "qty": canonical_decimal(qty),
                    "posting_at": posting_at.isoformat(),
                }
            ),
            item_id=key.item_id,
            characteristic_ref=key.characteristic_ref,
            organization_ref=key.organization_ref,
            warehouse_ref1c=key.warehouse_ref1c,
            qty=qty,
            qty_after=qty,
            posting_at=posting_at,
            record_type="Receipt" if qty >= 0 else "Expense",
            movement_kind=SEED_RECORDER_TYPE,
            recorder_type=SEED_RECORDER_TYPE,
            recorder_ref=recorder_ref,
            line_no="0",
            ingest_source=ingest_source,
        )
        session.add(entry)
        session.flush()  # assign entry.id
        created.append(entry)

        if ledger_generation_id is not None:
            rebuild_running_balance(
                session, key, ledger_generation_id=ledger_generation_id
            )

        session.add(
            models.StockLedgerAnchor(
                ingest_batch_id=import_batch.id,
                item_id=key.item_id,
                characteristic_ref=key.characteristic_ref,
                organization_ref=key.organization_ref,
                warehouse_ref1c=key.warehouse_ref1c,
                anchor_period=anchor_period,
                anchor_at=posting_at,
                balance_qty=qty,
                source="balance_seed",
                entry_id=entry.id,
            )
        )

    session.flush()
    return created
