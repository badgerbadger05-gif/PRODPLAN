"""Ledger-1 Balance-reconcile (the shrunk drift) + shadow diagnostics — §3б.

The local materialized ``stock_bin.on_hand`` may drift from 1С for postings made
OUTSIDE our documents (manual Списание, inventory counts, foreign transfers).
The existing ~30-min stock sweep already pulls the 1С ``/Balance`` snapshot and
full-refreshes ``ItemWarehouseStock``/``Item.stock_qty``; inc3 adds an
after-step that compares that snapshot against the ledger bins and, for
confirmed out-of-band deltas, writes a compensating adjustment-SLE.

This is the shrunk successor of today's drift model — NOT a re-implementation of
the norm-model. The maturity window W=48h is not needed (one registrar); only a
one-sweep debounce against snapshot races (§3б step 3):

* ``|delta| ≤ EPS``            → matched: ``last_reconciled_at``, clear pending.
* ``|delta| > EPS`` 1st sweep  → store ``reconcile_pending_qty=delta``; DON'T apply.
* ``|delta| > EPS`` 2nd sweep, same (±EPS) delta AND no in-flight pull touching
  the item → apply: INSERT adjustment-SLE (qty=delta,
  ingest_source='balance_reconcile', movement_kind='reconcile_adjustment',
  recorder_type='reconcile', recorder_ref=<batch guid>, posting_at=snapshot
  period), rebuild_running_balance + fold the bin, mark reconciled.

Everything here stays SHADOW: the adjustment-SLE feeds only the (still-unread)
stock_bin — no reader is switched (inc5), no reservation side is wired (inc4),
and there is no OData write (INV-1way / INV-no-write). The diagnostic report is
read-only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app import models
from .physical import EPS, LedgerKey, _dec, rebuild_running_balance

logger = logging.getLogger(__name__)

RECONCILE_SOURCE = "balance_reconcile"
RECONCILE_RECORDER_TYPE = "reconcile"
RECONCILE_MOVEMENT_KIND = "reconcile_adjustment"


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class ReconcileEvent:
    """One decided key in a reconcile sweep (kept for the sync log / report)."""

    key: LedgerKey
    balance_qty: Decimal
    on_hand: Decimal
    delta: Decimal
    action: str  # matched | pending | held | adjusted
    sle_id: Optional[int] = None
    note: str = ""


@dataclass
class ReconcileResult:
    """Counters + event trace of one reconcile_balance_snapshot call (§3б)."""

    batch_ref: str = ""
    snapshot_period: Optional[datetime] = None
    compared: int = 0
    matched: int = 0
    pending: int = 0  # first-seen deltas stored, not applied
    held: int = 0     # confirmed deltas held back by an in-flight pull
    adjusted: int = 0  # adjustment-SLEs written
    events: List[ReconcileEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# pending-pull race guard (§3б step 3)
# ---------------------------------------------------------------------------


def _has_inflight_pull(session: Session) -> bool:
    """True if any recorder pull can still mutate the ledger: status='pending'
    OR a retriable error (status='error' AND attempts < cap) — the same
    in-flight set process_pending_pulls will drain (ingest.is_inflight_pull).

    A queued document has moved 1С balance but its lines are not mirrored yet,
    so its item set is unknown until it drains — the exact snapshot race the
    debounce protects against. A retriable error pull is the same race
    stretched over retries: if reconcile applied the delta as an adjustment and
    the retry then inserted the same movements, the quantity would be counted
    twice until the next sweep compensated. We therefore treat ANY in-flight
    pull as blocking every item this sweep (coarse but honest: we cannot
    attribute an undrained pull to an item). Drained pulls (done/empty) and
    terminally failed ones (error, past the attempt cap) are NOT in-flight and
    never block — an exhausted pull will not retry, so the reconcile adjustment
    IS the recovery path for its movements.
    """
    from .ingest import DEFAULT_MAX_ATTEMPTS  # single home of the retry cap

    return (
        session.query(models.StockRecorderPull.id)
        .filter(
            or_(
                models.StockRecorderPull.status == "pending",
                and_(
                    models.StockRecorderPull.status == "error",
                    func.coalesce(models.StockRecorderPull.attempts, 0)
                    < DEFAULT_MAX_ATTEMPTS,
                ),
            )
        )
        .first()
        is not None
    )


# ---------------------------------------------------------------------------
# core: compare a Balance snapshot against the ledger bins
# ---------------------------------------------------------------------------


def reconcile_balance_snapshot(
    session: Session,
    snapshot: Mapping[LedgerKey, Any],
    *,
    snapshot_period: Optional[datetime] = None,
    batch_ref: Optional[str] = None,
    eps: Decimal = EPS,
    now: Optional[datetime] = None,
    block_all_items: Optional[bool] = None,
) -> ReconcileResult:
    """Compare a Balance snapshot ``{LedgerKey: qty}`` to the bins and reconcile.

    ``snapshot`` is already normalized to ledger keys (char=''), summed per key.
    Missing bin ⇒ on_hand 0; missing balance row ⇒ balance 0; keys that are zero
    on both sides are not persisted. Debounce + apply per §3б step 3. Writes only
    ledger-1 tables (stock_ledger_entry.qty_after via rebuild + stock_bin). No
    OData, no INSERT outside ledger-1.
    """
    now = now or datetime.now()
    snapshot_period = snapshot_period or now
    batch_ref = batch_ref or f"reconcile:{uuid.uuid4()}"
    result = ReconcileResult(batch_ref=batch_ref, snapshot_period=snapshot_period)

    normalized_snapshot: Dict[LedgerKey, Decimal] = {
        LedgerKey(*k): _dec(v) for k, v in snapshot.items()
    }

    bins_by_key: Dict[LedgerKey, models.StockBin] = {}
    for b in session.query(models.StockBin).all():
        bins_by_key[
            LedgerKey(
                int(b.item_id),
                b.characteristic_ref or "",
                b.organization_ref or "",
                b.warehouse_ref1c or "",
            )
        ] = b

    if block_all_items is None:
        block_all_items = _has_inflight_pull(session)

    keys: Set[LedgerKey] = set(bins_by_key.keys()) | set(normalized_snapshot.keys())
    line_seq = 0

    for key in sorted(keys):
        bin_row = bins_by_key.get(key)
        balance_qty = normalized_snapshot.get(key, Decimal("0"))
        on_hand = _dec(bin_row.on_hand) if bin_row is not None else Decimal("0")
        delta = balance_qty - on_hand

        # Both sides zero and no bin row → nothing to persist (§3б step 2).
        if bin_row is None and abs(balance_qty) <= eps:
            continue

        result.compared += 1

        if abs(delta) <= eps:
            # Matched: clear the debounce, stamp the reconcile time (§3б step 2).
            if bin_row is not None:
                bin_row.reconcile_pending_qty = Decimal("0")
                bin_row.last_reconciled_at = now
            result.matched += 1
            result.events.append(
                ReconcileEvent(key, balance_qty, on_hand, delta, "matched")
            )
            continue

        prior_pending = _dec(bin_row.reconcile_pending_qty) if bin_row is not None else Decimal("0")
        same_as_prior = abs(prior_pending) > eps and abs(prior_pending - delta) <= eps

        if same_as_prior and block_all_items:
            # Confirmed delta but a pull is in-flight → hold (snapshot race).
            bin_row.reconcile_pending_qty = delta
            result.held += 1
            result.events.append(
                ReconcileEvent(key, balance_qty, on_hand, delta, "held",
                               note="in-flight pull")
            )
            continue

        if same_as_prior:
            # Second consecutive sweep, same delta, no in-flight pull → APPLY.
            line_seq += 1
            sle = _insert_adjustment_sle(
                session, key, delta, snapshot_period, batch_ref, line_seq
            )
            session.flush()
            rebuild_running_balance(session, key)
            applied_bin = (
                session.query(models.StockBin)
                .filter(
                    models.StockBin.item_id == key.item_id,
                    models.StockBin.characteristic_ref == key.characteristic_ref,
                    models.StockBin.organization_ref == key.organization_ref,
                    models.StockBin.warehouse_ref1c == key.warehouse_ref1c,
                )
                .one()
            )
            applied_bin.reconcile_pending_qty = Decimal("0")
            applied_bin.last_reconciled_at = now
            result.adjusted += 1
            result.events.append(
                ReconcileEvent(key, balance_qty, on_hand, delta, "adjusted", sle_id=sle.id)
            )
            logger.info(
                "balance_reconcile applied adjustment sle=%s key=%s delta=%s "
                "(on_hand %s -> %s) batch=%s",
                sle.id, key, delta, on_hand, balance_qty, batch_ref,
            )
            continue

        # First sighting (or the delta changed / flipped sign → debounce reset):
        # store the pending delta, do NOT apply this sweep (§3б step 3).
        if bin_row is None:
            bin_row = models.StockBin(
                item_id=key.item_id,
                characteristic_ref=key.characteristic_ref,
                organization_ref=key.organization_ref,
                warehouse_ref1c=key.warehouse_ref1c,
                on_hand=Decimal("0"),
            )
            session.add(bin_row)
        bin_row.reconcile_pending_qty = delta
        result.pending += 1
        result.events.append(
            ReconcileEvent(key, balance_qty, on_hand, delta, "pending")
        )

    session.flush()
    return result


def _insert_adjustment_sle(
    session: Session,
    key: LedgerKey,
    delta: Decimal,
    snapshot_period: datetime,
    batch_ref: str,
    line_seq: int,
) -> models.StockLedgerEntry:
    """INSERT the compensating adjustment-SLE (§3б step 3). Signed qty=delta."""
    entry = models.StockLedgerEntry(
        item_id=key.item_id,
        characteristic_ref=key.characteristic_ref,
        organization_ref=key.organization_ref,
        warehouse_ref1c=key.warehouse_ref1c,
        qty=delta,
        qty_after=Decimal("0"),  # rebuild_running_balance fills this
        posting_at=snapshot_period,
        record_type="Receipt" if delta > 0 else "Expense",
        movement_kind=RECONCILE_MOVEMENT_KIND,
        recorder_type=RECONCILE_RECORDER_TYPE,
        recorder_ref=batch_ref,
        line_no=str(line_seq),
        ingest_source=RECONCILE_SOURCE,
    )
    session.add(entry)
    return entry


# ---------------------------------------------------------------------------
# sweep after-step: build the snapshot from converted Balance rows
# ---------------------------------------------------------------------------


def _resolve_item_maps(session: Session) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Return ({item_ref1c → item_id}, {norm_code → item_id}) for balance rows.

    Ref-first resolution matches how the puller keys bins (Item.item_ref1c), so a
    balance row and its bin land on the SAME item_id / ledger key.
    """
    from ..odata_stock_sync import _norm_code  # local import (avoid cycle)

    by_ref: Dict[str, int] = {}
    by_code: Dict[str, int] = {}
    for item_id, code, ref in session.query(
        models.Item.item_id, models.Item.item_code, models.Item.item_ref1c
    ).all():
        iid = int(item_id)
        r = str(ref or "").strip()
        if r:
            by_ref[r] = iid
        norm = _norm_code(str(code or ""))
        if norm:
            by_code.setdefault(norm, iid)
    return by_ref, by_code


def build_balance_snapshot(
    session: Session,
    balance_rows: Sequence[Mapping[str, Any]],
) -> Dict[LedgerKey, Decimal]:
    """Normalize converted Balance rows → ``{LedgerKey(char=''): qty}``.

    ``balance_rows`` is the ``get_stock_from_1c_odata`` shape ({code, ref,
    organization_ref, warehouse_ref, qty, ...}). Rows are summed per ledger key;
    a row whose item cannot be resolved is dropped (it has no bin either).
    """
    from ..odata_stock_sync import _norm_code

    by_ref, by_code = _resolve_item_maps(session)
    snapshot: Dict[LedgerKey, Decimal] = {}
    for row in balance_rows or []:
        wh = str(row.get("warehouse_ref") or "").strip()
        ref = str(row.get("ref") or "").strip()
        item_id: Optional[int] = by_ref.get(ref) if ref else None
        if item_id is None:
            norm = _norm_code(str(row.get("code") or ""))
            item_id = by_code.get(norm) if norm else None
        if item_id is None:
            continue
        org = str(row.get("organization_ref") or "").strip()
        key = LedgerKey(int(item_id), "", org, wh)
        snapshot[key] = snapshot.get(key, Decimal("0")) + _dec(row.get("qty") or 0)
    return snapshot


def run_balance_reconcile_after_sweep(
    session: Session,
    balance_rows: Sequence[Mapping[str, Any]],
    *,
    snapshot_period: Optional[datetime] = None,
    batch_ref: Optional[str] = None,
) -> ReconcileResult:
    """The stock-sweep after-step (§3б): reconcile bins vs the Balance snapshot.

    Called at the tail of ``sync_stock_from_odata`` with the FULL (pre-warehouse-
    filter) converted Balance rows, so bins in any known warehouse reconcile
    against 1С regardless of the planning selection contour. Guarded by its
    caller — a failure here never breaks the legacy sweep.
    """
    snapshot = build_balance_snapshot(session, balance_rows)
    return reconcile_balance_snapshot(
        session,
        snapshot,
        snapshot_period=snapshot_period,
        batch_ref=batch_ref,
    )


# ---------------------------------------------------------------------------
# shadow diagnostics (point 4) — read-only
# ---------------------------------------------------------------------------


def ledger_on_hand_by_item(session: Session) -> Dict[int, float]:
    """``{item_id: Σ stock_bin.on_hand}`` over the planning contour (design §2.5
    pool on_hand): selected warehouses, ignored excluded, **finished_goods
    (ГП) warehouses excluded** — the pool never sums stock parked on a
    finished-goods warehouse (§2.5: ГП выпускается напрямую на ГП-склад вне
    контура).

    This is the ledger-world stock source rendered per item so it can be laid
    beside the legacy world (shadow report) and, from Inc5, feed the flipped
    ``effective_stock_by_item_all`` bin path. Read-only. If no warehouse
    settings exist, sums every bin (legacy fallback).
    """
    ignored_refs = {
        str(r[0]) for r in session.query(models.IgnoredWarehouse.warehouse_ref1c).all()
        if r and r[0]
    }
    warehouse_rows = session.query(
        models.StockWarehouse.warehouse_ref1c,
        models.StockWarehouse.is_selected,
        models.StockWarehouse.is_finished_goods,
    ).all()
    # selected minus finished_goods — a ГП склад is out of the planning contour
    # even if it is (also) flagged selected (§2.5).
    selected_refs = {
        str(ref) for ref, sel, fg in warehouse_rows if ref and bool(sel) and not bool(fg)
    }
    finished_goods_refs = {str(ref) for ref, _sel, fg in warehouse_rows if ref and bool(fg)}
    has_settings = bool(warehouse_rows)

    q = session.query(
        models.StockBin.item_id, func.sum(models.StockBin.on_hand)
    )
    if has_settings:
        if selected_refs:
            q = q.filter(models.StockBin.warehouse_ref1c.in_(selected_refs))
        else:
            q = q.filter(False)
    if ignored_refs:
        q = q.filter(~models.StockBin.warehouse_ref1c.in_(ignored_refs))
    if finished_goods_refs:
        q = q.filter(~models.StockBin.warehouse_ref1c.in_(finished_goods_refs))
    rows = q.group_by(models.StockBin.item_id).all()
    return {int(iid): float(qty or 0.0) for iid, qty in rows}


def stock_shadow_report(
    session: Session,
    *,
    eps: float = float(EPS),
    include_all: bool = False,
) -> Dict[str, Any]:
    """Shadow-mode observation surface (point 4): ledger world vs legacy world.

    Per item: ``ledger_on_hand`` (Σ bin over the contour) vs ``legacy_stock``
    (``effective_stock_by_item_all``) and their divergence; plus reconcile
    counts (matched / pending / adjusted) over the bins. Read-only, no behavior
    change — this is what we watch ≥1 week before inc5 flips the readers.
    """
    from ..mrp_stock_helpers import effective_stock_by_item_all

    legacy = effective_stock_by_item_all(session)
    ledger = ledger_on_hand_by_item(session)

    item_codes = {
        int(iid): (code, name)
        for iid, code, name in session.query(
            models.Item.item_id, models.Item.item_code, models.Item.item_name
        ).all()
    }

    all_item_ids = set(legacy.keys()) | set(ledger.keys())
    items: List[Dict[str, Any]] = []
    divergent = 0
    tot_ledger = 0.0
    tot_legacy = 0.0
    for iid in all_item_ids:
        lv = float(ledger.get(iid, 0.0))
        gv = float(legacy.get(iid, 0.0))
        div = lv - gv
        tot_ledger += lv
        tot_legacy += gv
        is_div = abs(div) > eps
        if is_div:
            divergent += 1
        if include_all or is_div or abs(lv) > eps or abs(gv) > eps:
            code, name = item_codes.get(iid, ("", ""))
            items.append({
                "item_id": iid,
                "item_code": code,
                "item_name": name,
                "ledger_on_hand": lv,
                "legacy_stock": gv,
                "divergence": div,
            })
    items.sort(key=lambda r: abs(r["divergence"]), reverse=True)

    bins = session.query(models.StockBin).all()
    matched = sum(
        1 for b in bins
        if b.last_reconciled_at is not None and abs(float(b.reconcile_pending_qty or 0)) <= eps
    )
    pending = sum(1 for b in bins if abs(float(b.reconcile_pending_qty or 0)) > eps)

    adjustment_rows = (
        session.query(models.StockLedgerEntry)
        .filter(models.StockLedgerEntry.ingest_source == RECONCILE_SOURCE)
        .count()
    )
    adjusted_keys = (
        session.query(
            models.StockLedgerEntry.item_id,
            models.StockLedgerEntry.characteristic_ref,
            models.StockLedgerEntry.organization_ref,
            models.StockLedgerEntry.warehouse_ref1c,
        )
        .filter(models.StockLedgerEntry.ingest_source == RECONCILE_SOURCE)
        .distinct()
        .count()
    )

    return {
        "generated_at": datetime.now().isoformat(),
        "counts": {
            "bins": len(bins),
            "matched": matched,
            "pending": pending,
            "adjusted_keys": adjusted_keys,
            "adjustment_sles": adjustment_rows,
            "divergent_items": divergent,
        },
        "totals": {
            "ledger_on_hand": tot_ledger,
            "legacy_stock": tot_legacy,
            "divergence": tot_ledger - tot_legacy,
        },
        "items": items,
    }
