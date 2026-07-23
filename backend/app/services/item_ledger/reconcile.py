"""Ledger-1 Balance-reconcile v2 (сверка через документ-источник) — §3б + §7.4(д).

The local materialized ``stock_bin.on_hand`` may drift from 1С for postings made
OUTSIDE our documents (manual Списание, inventory counts, foreign transfers).
The existing ~30-min stock sweep already pulls the 1С ``/Balance`` snapshot and
full-refreshes ``ItemWarehouseStock``/``Item.stock_qty``; inc3 adds an
after-step that compares that snapshot against the ledger bins and, for
confirmed out-of-band deltas, recovers — v2: by *finding the missed document*
first, and only then (register clean) by a compensating adjustment-SLE.

Comparison axis — Д7, characteristics (choice documented per the audit):
    The Balance snapshot is pulled by ``get_stock_from_1c_odata`` with
    ``Dimensions='Номенклатура,СтруктурнаяЕдиница,Организация'`` — WITHOUT
    Характеристика — and ``convert_1c_stock_to_records`` does not surface a
    characteristic either. Widening those Dimensions was rejected: the same
    query feeds the legacy sweep consumers (``ItemWarehouseStock`` /
    ``Item.stock_qty``), so per-characteristic rows would change the granularity
    of a shared prod contract on unverified live-1С behavior (design Прил. A
    §3б step 1 pins the current dims). Therefore the reconcile compares
    **aggregates per (item, organization, warehouse)**: Σ ``stock_bin.on_hand``
    over ALL characteristics of the key vs the Balance row(s) for that key
    (variant «б»). Bins keyed by a real ``characteristic_ref`` (ingest keys bins
    by the register's Характеристика_Key) are никогда not compared against a
    char='' Balance row one-to-one — that produced a systematic false drift
    where every ~2 sweeps an adjustment pair «переливала» stock from the
    char-bin into the ''-bin. An adjustment (when it happens at all) is written
    ONLY into the char='' bin and ONLY for a matured *aggregate* discrepancy;
    per-char bins are never touched by the reconcile.

Debounce (§3б step 3) is unchanged: the maturity window W=48h is not needed
(one registrar); only a one-sweep debounce against snapshot races:

* ``|delta| ≤ EPS``            → matched: ``last_reconciled_at``, clear pending.
* ``|delta| > EPS`` 1st sweep  → store ``reconcile_pending_qty=delta``; DON'T apply.
* ``|delta| > EPS`` 2nd sweep, same (±EPS) delta AND no in-flight pull →
  the v2 discovery→adjustment order below.

Discovery→adjustment order (owner decision §7.4(д): «расхождение = мы
пропустили документ, а не повод для анонимной поправки»):

1. For a matured key, point-query the movements register
   ``AccumulationRegister_ЗапасыНаСкладах`` by Номенклатура (+ склад) with
   ``Period > последний якорь/последняя сверка`` (Инк0 mechanics) → recorders.
2. Recorders unknown locally (no ``stock_recorder_pull`` row, no SLE) →
   ``enqueue_recorder_pull(source='reconcile-discovery')`` and NO adjustment
   this sweep — the key is held until the orchestrator drains the queue and the
   staged pull-by-document replays the ledger with a real Recorder.
3. Register returned nothing new but the delta persists → an honest anonymous
   adjustment-SLE, counted as an ``anomaly`` (visible): qty=delta,
   ingest_source='balance_reconcile', movement_kind='reconcile_adjustment',
   recorder_type='reconcile', recorder_ref=<batch guid>, posting_at=snapshot
   period, rebuild_running_balance + fold the bin, mark reconciled.

Guards: the point query runs ONLY for matured discrepancies (never routinely);
at most ``RECONCILE_DISCOVERY_LIMIT`` discovery queries per sweep (the rest are
held, counted ``discovery_skipped``); an OData timeout/error holds the key
without an adjustment and never fails the sweep. Known limits: a document
backdated to ``Period ≤ since`` escapes the Period filter and ends as an
anomaly-adjustment (accepted, same as the design's backdating compromise); an
item without ``item_ref1c`` cannot be queried by name — the adjustment is its
only recovery path.

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
from .physical import (
    EPS,
    LedgerKey,
    _dec,
    canonical_content_hash,
    ensure_physical_import_batch,
    rebuild_running_balance,
)

logger = logging.getLogger(__name__)

RECONCILE_SOURCE = "balance_reconcile"
RECONCILE_RECORDER_TYPE = "reconcile"
RECONCILE_MOVEMENT_KIND = "reconcile_adjustment"

# §7.4(д) discovery: the pull-queue source tag and the per-sweep cap on point
# register queries (each matured key costs one OData round-trip; the rest are
# held to the next sweep — bounded 1С load by construction).
RECONCILE_DISCOVERY_SOURCE = "reconcile-discovery"
RECONCILE_DISCOVERY_LIMIT = 20

_ODATA_PREFIX = "StandardODATA."

# Sentinel: "build the real OData client lazily" (run_balance_reconcile_after_sweep).
_BUILD_CLIENT = object()


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class ReconcileEvent:
    """One decided key in a reconcile sweep (kept for the sync log / report).

    ``key`` is the AGGREGATE ledger key (char='') — the Д7 comparison axis.
    """

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
    held: int = 0     # confirmed deltas held back (in-flight pull / discovery)
    adjusted: int = 0  # adjustment-SLEs written
    # §7.4(д) discovery counters (sweep report):
    discovered_recorders: int = 0  # missed recorders found + enqueued this sweep
    discovery_skipped: int = 0     # matured keys not checked (limit / OData error)
    anomalies: int = 0             # register clean, delta stayed → anonymous adjustment
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

    Evaluated ONCE at sweep start: recorders enqueued by the discovery step of
    the same sweep do not retro-block other keys' decisions (each matured key
    runs its own discovery anyway).
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
# §7.4(д) discovery helpers
# ---------------------------------------------------------------------------


class _FailingDiscoveryClient:
    """Stand-in when the real OData client could not be built.

    Every discovery attempt raises, so matured deltas are HELD (§7.4д forbids
    an anonymous adjustment that skipped the document search) and the sweep
    itself never crashes.
    """

    def __init__(self, error: str) -> None:
        self._error = str(error)

    def get_all(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"discovery client unavailable: {self._error}")


def _norm_recorder_type(value: Any) -> str:
    """'StandardODATA.Document_X' / 'Document_X' → 'Document_X' (enqueue shape)."""
    s = str(value or "").strip()
    if s.startswith(_ODATA_PREFIX):
        s = s[len(_ODATA_PREFIX):]
    return s


def _extract_guid(value: Any) -> str:
    from .ingest import _norm_ref  # zero GUID → ''

    if isinstance(value, dict):
        value = value.get("Ref_Key") or value.get("RefKey") or value.get("ref_key") or ""
    return _norm_ref(value)


def _discover_register_recorders(
    client: Any,
    item_ref: str,
    warehouse_ref: str,
    since: Optional[datetime],
) -> List[Tuple[str, str]]:
    """Point-query the movements register: which recorders moved this item.

    Инк0 mechanics (same entity ingest pulls, same client): filter by
    ``Номенклатура_Key`` (+ ``СтруктурнаяЕдиница_Key`` when the key has a
    warehouse) and ``Period gt`` the last anchor/reconcile stamp. Returns
    unique ``(recorder_type, recorder_ref)`` pairs; raises on OData failure
    (the caller holds the key). Tolerates both response shapes: recorder-rows
    ``{Recorder, Recorder_Type, RecordSet}`` and flat movement lines carrying
    Recorder fields.
    """
    from .ingest import REGISTER_ENTITY

    parts = [f"Номенклатура_Key eq guid'{item_ref}'"]
    if warehouse_ref:
        parts.append(f"СтруктурнаяЕдиница_Key eq guid'{warehouse_ref}'")
    if since is not None:
        parts.append(
            f"Period gt datetime'{since.replace(microsecond=0).isoformat()}'"
        )
    rows = client.get_all(
        REGISTER_ENTITY, filter_query=" and ".join(parts), order_by=None
    )

    found: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for row in rows if isinstance(rows, list) else [rows]:
        if not isinstance(row, dict):
            continue
        ref = _extract_guid(row.get("Recorder"))
        rtype = _norm_recorder_type(row.get("Recorder_Type"))
        if not ref or not rtype or ref in seen:
            continue
        seen.add(ref)
        found.append((rtype, ref))
    return found


def _known_recorder_refs(session: Session, refs: Sequence[str]) -> Set[str]:
    """Subset of ``refs`` already visible locally: a stock_recorder_pull row
    (any status — pending/done/empty/error are all 'we know about it', so
    discovery never re-enqueues in a loop) or SLE lines under that recorder."""
    refs = [r for r in refs if r]
    if not refs:
        return set()
    known = {
        r[0]
        for r in session.query(models.StockRecorderPull.recorder_ref)
        .filter(models.StockRecorderPull.recorder_ref.in_(refs))
        .all()
    }
    known |= {
        r[0]
        for r in session.query(models.StockLedgerEntry.recorder_ref)
        .filter(models.StockLedgerEntry.recorder_ref.in_(refs))
        .distinct()
        .all()
    }
    return known


def _discovery_since(
    session: Session, key: LedgerKey, group: Sequence[models.StockBin]
) -> Optional[datetime]:
    """Lower Period bound for the point query: the last moment this aggregate
    key was provably consistent — max(last_reconciled_at over the group's bins,
    anchor_at over the key's anchors, any characteristic)."""
    stamps = [b.last_reconciled_at for b in group if b.last_reconciled_at is not None]
    anchor_at = (
        session.query(func.max(models.StockLedgerAnchor.anchor_at))
        .filter(
            models.StockLedgerAnchor.item_id == key.item_id,
            models.StockLedgerAnchor.organization_ref == key.organization_ref,
            models.StockLedgerAnchor.warehouse_ref1c == key.warehouse_ref1c,
        )
        .scalar()
    )
    if anchor_at is not None:
        stamps.append(anchor_at)
    return max(stamps) if stamps else None


# ---------------------------------------------------------------------------
# core: compare a Balance snapshot against the ledger bins
# ---------------------------------------------------------------------------


def _aggregate_key(key: LedgerKey) -> LedgerKey:
    """Д7 comparison axis: collapse the characteristic (Balance has none)."""
    return LedgerKey(key.item_id, "", key.organization_ref, key.warehouse_ref1c)


def _store_pending(
    session: Session,
    key: LedgerKey,
    group: List[models.StockBin],
    delta: Decimal,
    ledger_generation_id: int,
) -> models.StockBin:
    """Keep the debounce alive on the aggregate's char='' bin (Д7).

    The pending delta is stored on the ''-bin and cleared on the char-bins, so
    Σ ``reconcile_pending_qty`` over the group always equals the last-seen
    aggregate delta (also migrates any pre-Д7 per-char pending leftovers).
    """
    adj_bin = next((b for b in group if (b.characteristic_ref or "") == ""), None)
    if adj_bin is None:
        adj_bin = models.StockBin(
            ledger_generation_id=int(ledger_generation_id),
            item_id=key.item_id,
            characteristic_ref="",
            organization_ref=key.organization_ref,
            warehouse_ref1c=key.warehouse_ref1c,
            on_hand=Decimal("0"),
        )
        session.add(adj_bin)
        group.append(adj_bin)
    for b in group:
        b.reconcile_pending_qty = Decimal("0")
    adj_bin.reconcile_pending_qty = delta
    return adj_bin


def reconcile_balance_snapshot(
    session: Session,
    snapshot: Mapping[LedgerKey, Any],
    *,
    snapshot_period: Optional[datetime] = None,
    batch_ref: Optional[str] = None,
    eps: Decimal = EPS,
    now: Optional[datetime] = None,
    block_all_items: Optional[bool] = None,
    discovery_client: Any = None,
    discovery_limit: int = RECONCILE_DISCOVERY_LIMIT,
    import_batch: Optional[models.PhysicalImportBatch] = None,
    ledger_generation_id: Optional[int] = None,
) -> ReconcileResult:
    """Compare a Balance snapshot ``{LedgerKey: qty}`` to the bins and reconcile.

    Snapshot keys are normalized to the AGGREGATE axis (char='', summed per
    (item, org, warehouse) — Д7 variant «б»); bins are summed over all
    characteristics of the same aggregate key. Missing bin ⇒ on_hand 0; missing
    balance row ⇒ balance 0; keys that are zero on both sides are not
    persisted. Debounce + apply per §3б step 3; a matured delta goes through
    the §7.4(д) discovery order when ``discovery_client`` is set (the prod
    sweep always sets it; ``None`` = legacy direct-adjustment mode for
    unit-level callers). Writes only ledger-1 tables
    (stock_ledger_entry.qty_after via rebuild + stock_bin) plus
    ``stock_recorder_pull`` enqueues from discovery. No OData write.
    """
    now = now or datetime.now()
    snapshot_period = snapshot_period or now
    batch_ref = batch_ref or f"reconcile:{uuid.uuid4()}"
    if ledger_generation_id is None:
        raise ValueError("reconcile requires explicit ledger_generation_id")
    generation = session.get(
        models.LedgerGeneration, int(ledger_generation_id)
    )
    if generation is None:
        raise ValueError(
            f"reconcile Ledger generation {ledger_generation_id} does not exist"
        )
    if str(generation.status) != "building":
        raise ValueError(
            "reconcile may mutate only an explicit building Ledger generation"
        )
    result = ReconcileResult(batch_ref=batch_ref, snapshot_period=snapshot_period)

    normalized_snapshot: Dict[LedgerKey, Decimal] = {}
    for k, v in snapshot.items():
        agg = _aggregate_key(LedgerKey(*k))
        normalized_snapshot[agg] = normalized_snapshot.get(agg, Decimal("0")) + _dec(v)

    groups: Dict[LedgerKey, List[models.StockBin]] = {}
    for b in session.query(models.StockBin).filter(
        models.StockBin.ledger_generation_id == int(ledger_generation_id)
    ).all():
        agg = LedgerKey(
            int(b.item_id), "", b.organization_ref or "", b.warehouse_ref1c or ""
        )
        groups.setdefault(agg, []).append(b)

    if block_all_items is None:
        block_all_items = _has_inflight_pull(session)

    keys: Set[LedgerKey] = set(groups.keys()) | set(normalized_snapshot.keys())
    line_seq = 0
    discovery_budget = max(0, int(discovery_limit))

    for key in sorted(keys):
        group = groups.get(key, [])
        balance_qty = normalized_snapshot.get(key, Decimal("0"))
        on_hand = sum((_dec(b.on_hand) for b in group), Decimal("0"))
        delta = balance_qty - on_hand

        # Both sides zero and no bin rows → nothing to persist (§3б step 2).
        if not group and abs(balance_qty) <= eps:
            continue

        result.compared += 1

        if abs(delta) <= eps:
            # Matched: clear the debounce, stamp the reconcile time (§3б step 2)
            # on every bin of the aggregate — they are jointly reconciled.
            for b in group:
                b.reconcile_pending_qty = Decimal("0")
                b.last_reconciled_at = now
            result.matched += 1
            result.events.append(
                ReconcileEvent(key, balance_qty, on_hand, delta, "matched")
            )
            continue

        prior_pending = sum(
            (_dec(b.reconcile_pending_qty) for b in group), Decimal("0")
        )
        same_as_prior = abs(prior_pending) > eps and abs(prior_pending - delta) <= eps

        if same_as_prior and block_all_items:
            # Confirmed delta but a pull is in-flight → hold (snapshot race).
            _store_pending(session, key, group, delta, int(ledger_generation_id))
            result.held += 1
            result.events.append(
                ReconcileEvent(key, balance_qty, on_hand, delta, "held",
                               note="in-flight pull")
            )
            continue

        if same_as_prior:
            # Second consecutive sweep, same delta, no in-flight pull. §7.4(д):
            # a discrepancy means «we missed a document» — search the register
            # for its recorder BEFORE resorting to an anonymous adjustment.
            if discovery_client is not None:
                if discovery_budget <= 0:
                    result.discovery_skipped += 1
                    _store_pending(session, key, group, delta, int(ledger_generation_id))
                    result.held += 1
                    result.events.append(
                        ReconcileEvent(key, balance_qty, on_hand, delta, "held",
                                       note="discovery limit reached")
                    )
                    continue
                discovery_budget -= 1
                item_ref = (
                    session.query(models.Item.item_ref1c)
                    .filter(models.Item.item_id == key.item_id)
                    .scalar()
                )
                item_ref = str(item_ref or "").strip()
                if item_ref:
                    try:
                        recorders = _discover_register_recorders(
                            discovery_client,
                            item_ref,
                            key.warehouse_ref1c,
                            _discovery_since(session, key, group),
                        )
                    except Exception as exc:  # noqa: BLE001 — OData must not fail the sweep
                        logger.warning(
                            "balance_reconcile discovery failed for key=%s: %s",
                            key, exc,
                        )
                        result.discovery_skipped += 1
                        _store_pending(session, key, group, delta, int(ledger_generation_id))
                        result.held += 1
                        result.events.append(
                            ReconcileEvent(key, balance_qty, on_hand, delta, "held",
                                           note=f"discovery error: {exc}")
                        )
                        continue
                    known = _known_recorder_refs(
                        session, [ref for _t, ref in recorders]
                    )
                    unknown = [(t, ref) for t, ref in recorders if ref not in known]
                    if unknown:
                        # Missed document(s): enqueue the staged pull and hold —
                        # the orchestrator drains the queue; the replayed
                        # movements carry a real Recorder (no anonymous SLE).
                        from .ingest import enqueue_recorder_pull

                        for rtype, rref in unknown:
                            enqueue_recorder_pull(
                                session, rtype, rref,
                                source=RECONCILE_DISCOVERY_SOURCE,
                            )
                        result.discovered_recorders += len(unknown)
                        _store_pending(session, key, group, delta, int(ledger_generation_id))
                        result.held += 1
                        result.events.append(
                            ReconcileEvent(
                                key, balance_qty, on_hand, delta, "held",
                                note=f"discovery: {len(unknown)} recorder(s) enqueued",
                            )
                        )
                        logger.info(
                            "balance_reconcile discovery enqueued %d recorder(s) "
                            "for key=%s delta=%s batch=%s",
                            len(unknown), key, delta, batch_ref,
                        )
                        continue
                    # Register clean, delta persists → a true, visible anomaly.
                    result.anomalies += 1
                else:
                    # No 1С ref → the register cannot be queried by name; the
                    # adjustment is the only recovery path for this key.
                    result.anomalies += 1

            # APPLY: anonymous adjustment into the char='' bin (Д7 aggregate).
            line_seq += 1
            sle = _insert_adjustment_sle(
                session,
                key,
                delta,
                snapshot_period,
                batch_ref,
                line_seq,
                import_batch=import_batch,
            )
            session.flush()
            # A building generation advances to the newly imported physical
            # boundary. Accepted generations are rejected above and therefore
            # remain immutable and reproducible.
            generation.physical_import_batch_id = int(sle.ingest_batch_id)
            rebuild_running_balance(
                session, key, ledger_generation_id=int(ledger_generation_id)
            )
            applied_bin = (
                session.query(models.StockBin)
                .filter(
                    models.StockBin.item_id == key.item_id,
                    models.StockBin.ledger_generation_id
                    == int(ledger_generation_id),
                    models.StockBin.characteristic_ref == key.characteristic_ref,
                    models.StockBin.organization_ref == key.organization_ref,
                    models.StockBin.warehouse_ref1c == key.warehouse_ref1c,
                )
                .one()
            )
            for b in [*group, applied_bin]:
                b.reconcile_pending_qty = Decimal("0")
                b.last_reconciled_at = now
            result.adjusted += 1
            result.events.append(
                ReconcileEvent(key, balance_qty, on_hand, delta, "adjusted", sle_id=sle.id)
            )
            logger.info(
                "balance_reconcile applied adjustment sle=%s key=%s delta=%s "
                "(on_hand %s -> %s) batch=%s",
                sle.id, key, delta, on_hand, balance_qty, batch_ref,
            )
            # Trigger т1 (design §5 / §6.1 «adjustment сверки → redistribute»):
            # the adjustment moved on_hand, so refresh the touched item's coverage
            # caches (a negative delta may surface uncovered — пример 3). match=False
            # — an anonymous adjustment has no reservation to realize; matching stays
            # the cycle's job. Guarded internally: never breaks the sweep.
            from .reservation_ledger import redistribute_after_ledger_apply

            redistribute_after_ledger_apply(
                session, [key.item_id], batch_ref[:64], match=False
            )
            continue

        # First sighting (or the delta changed / flipped sign → debounce reset):
        # store the pending delta, do NOT apply this sweep (§3б step 3).
        _store_pending(session, key, group, delta, int(ledger_generation_id))
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
    *,
    import_batch: Optional[models.PhysicalImportBatch] = None,
) -> models.StockLedgerEntry:
    """INSERT the compensating adjustment-SLE (§3б step 3). Signed qty=delta."""
    row_hash = canonical_content_hash(
        {
            "recorder_type": RECONCILE_RECORDER_TYPE,
            "recorder_ref": batch_ref,
            "line_no": str(line_seq),
            "ledger_key": list(key),
            "qty": str(delta.normalize()),
            "posting_at": snapshot_period.isoformat(),
        }
    )
    import_batch = ensure_physical_import_batch(
        session,
        batch_key=f"reconcile:{canonical_content_hash(batch_ref)[:24]}:{row_hash}",
        cutoff=snapshot_period,
        source_watermarks={
            "source": RECONCILE_SOURCE,
            "batch_ref": batch_ref,
            "content_hash": row_hash,
        },
        batch=import_batch,
    )
    entry = models.StockLedgerEntry(
        ingest_batch_id=import_batch.id,
        source_content_hash=row_hash,
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
    *,
    strict: bool = False,
) -> Dict[LedgerKey, Decimal]:
    """Normalize converted Balance rows → ``{LedgerKey(char=''): qty}``.

    ``balance_rows`` is the ``get_stock_from_1c_odata`` shape ({code, ref,
    organization_ref, warehouse_ref, qty, ...}) — aggregate per (item, org,
    warehouse), no characteristic dimension (Д7 variant «б», see module
    docstring). Rows are summed per ledger key; a row whose item cannot be
    resolved is dropped (it has no bin either).
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
            if strict and abs(_dec(row.get("qty") or 0)) > EPS:
                identity = ref or str(row.get("code") or "").strip() or "<missing>"
                raise ValueError(
                    f"Balance row item cannot be resolved locally: {identity}"
                )
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
    discovery_client: Any = _BUILD_CLIENT,
    discovery_limit: int = RECONCILE_DISCOVERY_LIMIT,
    import_batch: Optional[models.PhysicalImportBatch] = None,
    ledger_generation_id: Optional[int] = None,
) -> ReconcileResult:
    """The stock-sweep after-step (§3б): reconcile bins vs the Balance snapshot.

    Called at the tail of ``sync_stock_from_odata`` with the FULL (pre-warehouse-
    filter) converted Balance rows, so bins in any known warehouse reconcile
    against 1С regardless of the planning selection contour. Guarded by its
    caller — a failure here never breaks the legacy sweep.

    Discovery (§7.4д) is ALWAYS on for this prod path: by default a read-only
    OData client is built lazily (same config as ingest); if it cannot be built,
    matured deltas are held rather than anonymously adjusted (never crash).
    Tests inject ``discovery_client`` explicitly; passing ``None`` disables
    discovery (legacy direct-adjustment mode).
    """
    if discovery_client is _BUILD_CLIENT:
        try:
            from .ingest import _build_client

            discovery_client = _build_client()
        except Exception as exc:  # noqa: BLE001 — held, not crashed (§7.4д)
            discovery_client = _FailingDiscoveryClient(str(exc))
    snapshot = build_balance_snapshot(session, balance_rows)
    return reconcile_balance_snapshot(
        session,
        snapshot,
        snapshot_period=snapshot_period,
        batch_ref=batch_ref,
        discovery_client=discovery_client,
        discovery_limit=discovery_limit,
        import_batch=import_batch,
        ledger_generation_id=ledger_generation_id,
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


def contour_warehouse_refs(session: Session) -> Set[str]:
    """Refs of warehouses INSIDE the planning contour (design §2.5): selected,
    NOT ignored, NOT finished_goods — the same axis :func:`ledger_on_hand_by_item`
    sums on_hand over.

    Used to classify a ``ПеремещениеЗапасов`` (task 2): a ``transfer_out`` whose
    paired ``transfer_in`` lands on one of THESE warehouses is an INTERNAL pool
    move (the detail never left the contour) and must NOT realize a consume
    reserve. A transfer leaving the contour (workshop / external / ГП) does.

    Returns a POSITIVE set: only warehouses we can confirm are in-contour. When
    no warehouse settings exist at all the set is empty — the internal-move
    suppression then never fires and ``transfer_out`` keeps its legacy
    realize-always behavior (conservative: суppress only on a proven contour
    destination). ГП-склады are excluded even when also flagged selected (§2.5).
    """
    ignored_refs = {
        str(r[0]) for r in session.query(models.IgnoredWarehouse.warehouse_ref1c).all()
        if r and r[0]
    }
    rows = session.query(
        models.StockWarehouse.warehouse_ref1c,
        models.StockWarehouse.is_selected,
        models.StockWarehouse.is_finished_goods,
    ).all()
    return {
        str(ref)
        for ref, sel, fg in rows
        if ref and bool(sel) and not bool(fg) and str(ref) not in ignored_refs
    }


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
