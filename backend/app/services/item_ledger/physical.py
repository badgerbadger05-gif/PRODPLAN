"""Ledger-1 (physical movements) math + seeding — design §2.1, §3, §6.

Pure helper (`fold_running_balance`) plus two Session-taking writers that only
touch the new ledger tables (`rebuild_running_balance`, `seed_from_balance`).
No OData, no writes into any pre-existing table (INV-1way / INV-no-write).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, List, Mapping, NamedTuple, Optional, Tuple, Union

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app import models

EPS = Decimal("1e-9")

Number = Union[int, float, Decimal, str]


def _dec(value: Number) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class LedgerKey(NamedTuple):
    """Physical key of ledger-1 (design §2): the axis Balance-сверка can see."""

    item_id: int
    characteristic_ref: str = ""
    organization_ref: str = ""
    warehouse_ref1c: str = ""


def fold_running_balance(
    qtys: Iterable[Number],
    start: Number = Decimal("0"),
) -> Tuple[List[Decimal], Decimal]:
    """PURE fold: signed movement quantities → (running qty_after list, final).

    on_hand = Σ qty = final = last qty_after (design §2.1 R-A / §6). No clamping
    of negatives — a negative running balance is a real reconcile signal (§4a).
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
) -> Decimal:
    """Recompute qty_after forward for one ledger key and refold stock_bin.on_hand.

    Rows are ordered by (posting_at, id) — insertion order within a timestamp.
    ``from_posting_at`` limits the rewrite window: the opening balance is the
    qty_after of the last row strictly before it (design §6 narrow rebuild).
    Returns the final on_hand. Writes only stock_ledger_entry.qty_after and
    stock_bin (both ledger-1 tables).
    """
    ledger_key = LedgerKey(*ledger_key)
    base_filter = and_(
        models.StockLedgerEntry.item_id == ledger_key.item_id,
        models.StockLedgerEntry.characteristic_ref == ledger_key.characteristic_ref,
        models.StockLedgerEntry.organization_ref == ledger_key.organization_ref,
        models.StockLedgerEntry.warehouse_ref1c == ledger_key.warehouse_ref1c,
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

    bin_row = (
        session.query(models.StockBin)
        .filter(
            models.StockBin.item_id == ledger_key.item_id,
            models.StockBin.characteristic_ref == ledger_key.characteristic_ref,
            models.StockBin.organization_ref == ledger_key.organization_ref,
            models.StockBin.warehouse_ref1c == ledger_key.warehouse_ref1c,
        )
        .one_or_none()
    )
    if bin_row is None:
        bin_row = models.StockBin(
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


def seed_from_balance(
    session: Session,
    balance_snapshot: Mapping[LedgerKey, Number],
    anchor_period: date,
    posting_at: Optional[datetime] = None,
    ingest_source: str = "seed",
) -> List[models.StockLedgerEntry]:
    """Seed ledger-1 from a Balance dict {ledger_key: qty} (design §2.1 seed / §6).

    Writes one seed SLE per key (movement_kind='seed', qty_after == qty), builds
    the stock_bin, and records a stock_ledger_anchor for the period. The ACTUAL
    OData Balance pull is inc2 — here only the seeding logic given a dict is
    exercised (no OData). Idempotent per (period, key) via the recorder unique
    key and the anchor unique key; re-seeding a present key is skipped.
    """
    posting_at = posting_at or datetime.combine(anchor_period, datetime.min.time())
    created: List[models.StockLedgerEntry] = []

    for raw_key, raw_qty in balance_snapshot.items():
        key = LedgerKey(*raw_key)
        qty = _dec(raw_qty)
        recorder_ref = (
            f"{anchor_period.isoformat()}:{key.item_id}:{key.characteristic_ref}:"
            f"{key.organization_ref}:{key.warehouse_ref1c}"
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
            item_id=key.item_id,
            characteristic_ref=key.characteristic_ref,
            organization_ref=key.organization_ref,
            warehouse_ref1c=key.warehouse_ref1c,
            qty=qty,
            qty_after=qty,
            posting_at=posting_at,
            record_type="Receipt" if qty >= 0 else "Expense",
            movement_kind="seed",
            recorder_type="seed",
            recorder_ref=recorder_ref,
            line_no="0",
            ingest_source=ingest_source,
        )
        session.add(entry)
        session.flush()  # assign entry.id
        created.append(entry)

        bin_row = (
            session.query(models.StockBin)
            .filter(
                models.StockBin.item_id == key.item_id,
                models.StockBin.characteristic_ref == key.characteristic_ref,
                models.StockBin.organization_ref == key.organization_ref,
                models.StockBin.warehouse_ref1c == key.warehouse_ref1c,
            )
            .one_or_none()
        )
        if bin_row is None:
            bin_row = models.StockBin(
                item_id=key.item_id,
                characteristic_ref=key.characteristic_ref,
                organization_ref=key.organization_ref,
                warehouse_ref1c=key.warehouse_ref1c,
            )
            session.add(bin_row)
        bin_row.on_hand = qty
        bin_row.last_entry_id = entry.id

        session.add(
            models.StockLedgerAnchor(
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
