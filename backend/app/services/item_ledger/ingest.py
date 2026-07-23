"""Ledger-1 physical ingest — pull-by-document (design §2.1, §2.3, §3а, §6).

Mirrors 1С ``AccumulationRegister_ЗапасыНаСкладах`` movement lines into the
append-only ``stock_ledger_entry`` (ledger-1) READ-ONLY: this module contains no
OData write and never touches a pre-existing planning table (INV-1way /
INV-no-write). The confirmed 1С shape (Inc0 probe) is built to exactly:

* Query the register filtered ``Recorder eq cast(guid'REF', '<recorder_type>')``
  (the cast form works; ``Recorder_Key eq guid`` → HTTP 400). The response is one
  recorder-row ``{Recorder, Recorder_Type, RecordSet}``; the movement lines are
  the nested ``RecordSet`` array.
* Per line: sign from ``RecordType`` (Receipt → +, Expense → −), qty from
  ``Количество`` (base UoM), item via ``Номенклатура_Key`` → ``Item.item_ref1c``,
  warehouse via ``СтруктурнаяЕдиница_Key`` → ``StockWarehouse.warehouse_ref1c``,
  characteristic ``Характеристика_Key`` (zero GUID → ''), org ``Организация_Key``,
  ``LineNumber`` → line_no, ``Period`` → posting_at, and ``Active`` must be true.

Dirt filter (§6): non-warehouse СтруктурнаяЕдиница (polymorphic, e.g. Контрагенты)
→ ``skipped_non_warehouse``; qty == 0 → dropped (no zero rows); unknown item →
``skipped_unknown_item`` + diagnostic (no crash). Replace-by-recorder (§3а step 4)
under a per-recorder advisory lock: delete this recorder's document_pull rows,
insert the normalized lines, then rebuild running balance + stock_bin for every
touched ledger key. Anchor guard skips lines at/under the active anchor T0.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import models
from .physical import LedgerKey, _dec, rebuild_running_balance

# Register + document-pull tags.
REGISTER_ENTITY = "AccumulationRegister_ЗапасыНаСкладах"
INGEST_SOURCE = "document_pull"
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"

# Retry cap for process_pending_pulls (attempts beyond this are not re-tried).
# ``attempts`` counts FAILED pull attempts only (since the last enqueue):
# success paths (done/empty) never bump it, the error path does, and
# enqueue_recorder_pull resets it — so the cap always means "N failures in a
# row", never "N pulls total".
DEFAULT_MAX_ATTEMPTS = 5


def is_retryable_error(status: Any, attempts: Any, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> bool:
    """True for a failed pull that is still under the retry cap.

    THE shared predicate for "this error row will be retried": used by the
    queue drain filter (process_pending_pulls), the reconcile in-flight guard
    (reconcile._has_inflight_pull) and the orchestrator's pull_queue_health —
    keep them in lock-step by changing only this function.
    """
    return str(status) == "error" and int(attempts or 0) < int(max_attempts)


def is_inflight_pull(status: Any, attempts: Any, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> bool:
    """True while a queued recorder can still mutate the ledger.

    in-flight = pending OR (error AND attempts < cap). Exhausted errors and
    drained pulls (done/empty) are terminal — they will not insert movements
    unless explicitly re-enqueued (which resets attempts).
    """
    return str(status) == "pending" or is_retryable_error(status, attempts, max_attempts)


@dataclass
class PullResult:
    """Outcome of one pull_recorder_movements call (counters + status)."""

    recorder_type: str = ""
    recorder_ref: str = ""
    status: str = "empty"  # done | empty | error
    inserted: int = 0
    deleted: int = 0
    skipped_non_warehouse: int = 0
    skipped_unknown_item: int = 0
    skipped_zero_qty: int = 0
    skipped_pre_anchor: int = 0
    skipped_inactive: int = 0
    skipped_unknown_record_type: int = 0
    touched_keys: List[LedgerKey] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _norm_ref(value: Any) -> str:
    """Normalize a 1С GUID ref: strip; zero GUID → '' (design §2.1)."""
    s = str(value or "").strip()
    if not s or s == EMPTY_GUID:
        return ""
    return s


def _parse_period(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _movement_kind(recorder_type: str, record_type: str) -> str:
    is_receipt = record_type == "Receipt"
    if "Перемещение" in recorder_type:
        return "transfer_in" if is_receipt else "transfer_out"
    if "Сборка" in recorder_type:
        return "assembly_in" if is_receipt else "assembly_out"
    return "receipt" if is_receipt else "expense"


def _stable_lock_key(recorder_ref: str) -> int:
    """Deterministic signed 64-bit key for pg_advisory_xact_lock(bigint)."""
    digest = hashlib.sha1((recorder_ref or "").encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _advisory_xact_lock(session: Session, recorder_ref: str) -> None:
    """Per-recorder advisory lock on PostgreSQL; no-op elsewhere (e.g. SQLite)."""
    bind = session.get_bind()
    try:
        dialect = bind.dialect.name
    except Exception:
        dialect = ""
    if dialect == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": _stable_lock_key(recorder_ref)},
        )


def _build_client(config: Optional[Mapping[str, Any]] = None):
    """Build a read-only OData client from odata_config (like the sync services)."""
    from ..odata_client import OData1CClient
    from ..odata_config import load_odata_config, sanitize_base_url

    cfg = dict(config or load_odata_config())
    base_url = sanitize_base_url(str(cfg.get("base_url") or ""))
    if not base_url:
        raise ValueError("OData config is not set (base_url missing).")
    return OData1CClient(
        base_url=base_url,
        username=cfg.get("username") or None,
        password=cfg.get("password") or None,
        token=cfg.get("token") or None,
    )


def _item_ref_map(session: Session) -> Dict[str, int]:
    """{item_ref1c → item_id} — the same mapping the stock sync uses (item_ref1c)."""
    rows = (
        session.query(models.Item.item_id, models.Item.item_ref1c)
        .filter(models.Item.item_ref1c.isnot(None))
        .all()
    )
    out: Dict[str, int] = {}
    for item_id, ref in rows:
        r = _norm_ref(ref)
        if r:
            out[r] = int(item_id)
    return out


def _warehouse_ref_set(session: Session) -> set:
    """Known StockWarehouse ref1c set — the dirt filter for non-warehouse units."""
    rows = session.query(models.StockWarehouse.warehouse_ref1c).all()
    return {_norm_ref(r[0]) for r in rows if _norm_ref(r[0])}


def _anchor_t0(session: Session, key: LedgerKey, cache: Dict[LedgerKey, Optional[datetime]]):
    if key in cache:
        return cache[key]
    t0 = (
        session.query(func.max(models.StockLedgerAnchor.anchor_at))
        .filter(
            models.StockLedgerAnchor.item_id == key.item_id,
            models.StockLedgerAnchor.characteristic_ref == key.characteristic_ref,
            models.StockLedgerAnchor.organization_ref == key.organization_ref,
            models.StockLedgerAnchor.warehouse_ref1c == key.warehouse_ref1c,
        )
        .scalar()
    )
    cache[key] = t0
    return t0


def _document_order_ref(client: Any, recorder_type: str, recorder_ref: str) -> str:
    """Fetch the recorder DOCUMENT HEADER and extract its production-order GUID.

    Variant-B substrate for the SLE→reservation matching chain (design §6.3):
    * ``Document_СборкаЗапасов`` carries ``ЗаказНаПроизводство_Key`` (the field
      our manufacture export fills and the 1C UI fills for hand-made docs);
    * ``Document_ПеремещениеЗапасов`` carries the composite ``ДокументОснование``
      — taken only when ``ДокументОснование_Type`` is ЗаказНаПроизводство.

    Best-effort: any OData/parsing failure returns '' and NEVER breaks the pull
    (the sync_link path and the reconcile Balance-sweep are the safety nets).
    """
    try:
        if "СборкаЗапасов" in recorder_type:
            select_fields = ["Ref_Key", "ЗаказНаПроизводство_Key"]
        elif "Перемещение" in recorder_type:
            select_fields = ["Ref_Key", "ДокументОснование", "ДокументОснование_Type"]
        else:
            return ""
        rows = client.get_all(
            recorder_type,
            filter_query=f"Ref_Key eq guid'{recorder_ref}'",
            select_fields=select_fields,
            order_by=None,
        )
        for row in rows if isinstance(rows, list) else [rows]:
            if not isinstance(row, dict):
                continue
            if "СборкаЗапасов" in recorder_type:
                ref = _norm_ref(row.get("ЗаказНаПроизводство_Key"))
                if ref:
                    return ref
            else:
                basis_type = str(row.get("ДокументОснование_Type") or "")
                if "ЗаказНаПроизводство" in basis_type:
                    ref = _norm_ref(row.get("ДокументОснование"))
                    if ref:
                        return ref
    except Exception:  # noqa: BLE001 — header capture is best-effort by design
        return ""
    return ""


def _extract_record_set(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten the RecordSet arrays across the returned recorder-row(s)."""
    lines: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rs = row.get("RecordSet")
        if isinstance(rs, list):
            lines.extend(x for x in rs if isinstance(x, dict))
    return lines


# ---------------------------------------------------------------------------
# core: pull one recorder
# ---------------------------------------------------------------------------


def pull_recorder_movements(
    session: Session,
    recorder_type: str,
    recorder_ref: str,
    *,
    client: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    source: Optional[str] = None,
) -> PullResult:
    """Pull one 1С recorder's RecordSet → ledger-1 (design §3а steps 1–5).

    Replace-by-recorder is atomic under a per-recorder advisory lock: the
    recorder's prior ``document_pull`` rows are deleted and the freshly
    normalized lines inserted, so a re-pull of the same recorder yields an
    identical row set (INV-idem). Running balance + stock_bin are refolded for
    every touched ledger key. Updates the stock_recorder_pull status/attempts.
    Raises on OData / DB failure (process_pending_pulls handles the rollback).
    """
    recorder_type = str(recorder_type or "")
    recorder_ref = _norm_ref(recorder_ref)
    result = PullResult(recorder_type=recorder_type, recorder_ref=recorder_ref)

    if not recorder_ref:
        raise ValueError("recorder_ref is required for pull_recorder_movements")

    if client is None:
        client = _build_client(config)

    filter_query = f"Recorder eq cast(guid'{recorder_ref}', '{recorder_type}')"
    rows = client.get_all(REGISTER_ENTITY, filter_query=filter_query, order_by=None)
    lines = _extract_record_set(rows if isinstance(rows, list) else [rows])

    # Variant-B header capture (design §6.3): remember the producing order's
    # GUID alongside the recorder so matching pegs 1C-native documents too.
    header_order_ref = _document_order_ref(client, recorder_type, recorder_ref)

    item_by_ref = _item_ref_map(session)
    warehouses = _warehouse_ref_set(session)
    anchor_cache: Dict[LedgerKey, Optional[datetime]] = {}

    # --- normalize (dirt filter + anchor guard) ---
    normalized: List[Tuple[LedgerKey, Decimal, str, str, datetime]] = []
    for line in lines:
        if line.get("Active") is False:
            result.skipped_inactive += 1
            continue

        wh = _norm_ref(line.get("СтруктурнаяЕдиница_Key"))
        if wh not in warehouses:
            result.skipped_non_warehouse += 1
            continue

        record_type = str(line.get("RecordType") or "")
        if record_type == "Receipt":
            sign = Decimal("1")
        elif record_type == "Expense":
            sign = Decimal("-1")
        else:
            result.skipped_unknown_record_type += 1
            continue

        item_ref = _norm_ref(line.get("Номенклатура_Key"))
        item_id = item_by_ref.get(item_ref)
        if item_id is None:
            result.skipped_unknown_item += 1
            if len(result.diagnostics) < 50:
                result.diagnostics.append(
                    f"unknown item ref {item_ref or '<empty>'} "
                    f"(line {line.get('LineNumber')}) in {recorder_type} {recorder_ref}"
                )
            continue

        signed_qty = sign * _dec(line.get("Количество") or 0)
        if signed_qty == 0:
            result.skipped_zero_qty += 1
            continue

        char = _norm_ref(line.get("Характеристика_Key"))
        org = _norm_ref(line.get("Организация_Key"))
        line_no = str(line.get("LineNumber") or "")
        posting_at = _parse_period(line.get("Period")) or datetime.now()

        key = LedgerKey(int(item_id), char, org, wh)

        t0 = _anchor_t0(session, key, anchor_cache)
        if t0 is not None and posting_at <= t0:
            result.skipped_pre_anchor += 1
            continue

        normalized.append((key, signed_qty, record_type, line_no, posting_at))

    # --- replace-by-recorder under advisory lock (§3а step 4) ---
    _advisory_xact_lock(session, recorder_ref)

    existing_key_rows = (
        session.query(
            models.StockLedgerEntry.item_id,
            models.StockLedgerEntry.characteristic_ref,
            models.StockLedgerEntry.organization_ref,
            models.StockLedgerEntry.warehouse_ref1c,
        )
        .filter(
            models.StockLedgerEntry.recorder_type == recorder_type,
            models.StockLedgerEntry.recorder_ref == recorder_ref,
            models.StockLedgerEntry.ingest_source == INGEST_SOURCE,
        )
        .distinct()
        .all()
    )
    touched: Dict[LedgerKey, None] = {
        LedgerKey(int(r[0]), r[1] or "", r[2] or "", r[3] or ""): None
        for r in existing_key_rows
    }

    # Д2 (design §6.1 last matrix row / §8): the delete below replaces this
    # recorder's SLE rows with fresh ids; realize events referencing the old
    # rows lose their applied-mark (FK sle_id ON DELETE SET NULL) and the fresh
    # rows would realize AGAIN — doubling realized_qty. Compensate with
    # unrealize events BEFORE the delete, while sle_id still resolves; the new
    # rows are then re-matched by the regular realize_from_sle pass.
    replaced_sle_ids = [
        int(sid)
        for (sid,) in session.query(models.StockLedgerEntry.id)
        .filter(
            models.StockLedgerEntry.recorder_type == recorder_type,
            models.StockLedgerEntry.recorder_ref == recorder_ref,
            models.StockLedgerEntry.ingest_source == INGEST_SOURCE,
        )
        .all()
    ]
    if replaced_sle_ids:
        from .reservation_ledger import unrealize_replaced_sle

        unrealize_replaced_sle(session, replaced_sle_ids, recorder_ref)

    result.deleted = (
        session.query(models.StockLedgerEntry)
        .filter(
            models.StockLedgerEntry.recorder_type == recorder_type,
            models.StockLedgerEntry.recorder_ref == recorder_ref,
            models.StockLedgerEntry.ingest_source == INGEST_SOURCE,
        )
        .delete(synchronize_session=False)
    )

    for key, signed_qty, record_type, line_no, posting_at in normalized:
        session.add(
            models.StockLedgerEntry(
                item_id=key.item_id,
                characteristic_ref=key.characteristic_ref,
                organization_ref=key.organization_ref,
                warehouse_ref1c=key.warehouse_ref1c,
                qty=signed_qty,
                qty_after=Decimal("0"),
                posting_at=posting_at,
                record_type=record_type,
                movement_kind=_movement_kind(recorder_type, record_type),
                recorder_type=recorder_type,
                recorder_ref=recorder_ref,
                line_no=line_no,
                ingest_source=INGEST_SOURCE,
            )
        )
        touched[key] = None
    session.flush()

    result.inserted = len(normalized)
    for key in touched:
        rebuild_running_balance(session, key)
    result.touched_keys = list(touched.keys())

    # --- pull-status transition (§2.3) ---
    # Success paths (done/empty) do NOT bump attempts: attempts counts failed
    # pull attempts only (the error path in process_pending_pulls bumps it).
    result.status = "done" if result.inserted > 0 else "empty"
    pull_row = _upsert_pull_row(
        session,
        recorder_type,
        recorder_ref,
        status=result.status,
        line_count=result.inserted,
        source=source,
        last_error=None,
    )
    # Refresh the captured header order ref on every (re-)pull; keep the last
    # known value when the header fetch failed this time (best-effort).
    if header_order_ref:
        pull_row.order_ref = header_order_ref
    session.flush()
    return result


# ---------------------------------------------------------------------------
# queue + retry (design §2.3 / §3а step 4)
# ---------------------------------------------------------------------------


def _upsert_pull_row(
    session: Session,
    recorder_type: str,
    recorder_ref: str,
    *,
    status: str,
    line_count: Optional[int] = None,
    source: Optional[str] = None,
    bump_attempt: bool = False,
    last_error: Optional[str] = None,
) -> models.StockRecorderPull:
    row = (
        session.query(models.StockRecorderPull)
        .filter(
            models.StockRecorderPull.recorder_type == recorder_type,
            models.StockRecorderPull.recorder_ref == recorder_ref,
        )
        .one_or_none()
    )
    if row is None:
        row = models.StockRecorderPull(
            recorder_type=recorder_type,
            recorder_ref=recorder_ref,
            attempts=0,
            line_count=0,
            source=source or "",
        )
        session.add(row)
    row.status = status
    if line_count is not None:
        row.line_count = int(line_count)
    if source:
        row.source = source
    if bump_attempt:
        row.attempts = int(row.attempts or 0) + 1
    row.last_error = last_error
    row.pulled_at = datetime.now()
    return row


def enqueue_recorder_pull(
    session: Session,
    recorder_type: str,
    recorder_ref: str,
    source: str = "",
) -> models.StockRecorderPull:
    """Put a recorder on the pull queue (design §3а step 1) — fast, no OData.

    Get-or-create equivalent of ``INSERT ... ON CONFLICT(recorder) DO UPDATE
    status='pending'``: a new recorder is inserted 'pending'; an existing row is
    reset to 'pending' so process_pending_pulls re-drains it. Never does the
    OData pull itself, so an export hook calling this never blocks on 1С.
    """
    recorder_type = str(recorder_type or "")
    recorder_ref = _norm_ref(recorder_ref)
    if not recorder_ref:
        raise ValueError("recorder_ref is required for enqueue_recorder_pull")

    row = (
        session.query(models.StockRecorderPull)
        .filter(
            models.StockRecorderPull.recorder_type == recorder_type,
            models.StockRecorderPull.recorder_ref == recorder_ref,
        )
        .one_or_none()
    )
    if row is None:
        row = models.StockRecorderPull(
            recorder_type=recorder_type,
            recorder_ref=recorder_ref,
            status="pending",
            attempts=0,
            line_count=0,
            source=source or "",
        )
        session.add(row)
    else:
        row.status = "pending"
        # A new export/reconcile event is a fresh request, not another retry of
        # the old failed pull. Otherwise an exhausted row could never re-enter
        # the bounded worker queue.
        row.attempts = 0
        row.last_error = None
        if source:
            row.source = source
    session.flush()
    return row


def process_pending_pulls(
    session: Session,
    *,
    client: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    limit: Optional[int] = None,
) -> List[PullResult]:
    """Drain queued recorders (status in {pending, error}, under the attempt cap).

    Each recorder is pulled + committed independently; a failure rolls back and
    records the error (status='error', attempts++, last_error) in its own
    transaction so one bad recorder never poisons the batch. Safe for the
    reconcile/sync worker to call — it does NOT run on the export path.
    """
    if client is None:
        client = _build_client(config)

    query = (
        session.query(models.StockRecorderPull)
        .filter(
            models.StockRecorderPull.status.in_(["pending", "error"]),
            models.StockRecorderPull.attempts < int(max_attempts),
        )
        .order_by(models.StockRecorderPull.id.asc())
    )
    if limit is not None:
        query = query.limit(int(limit))
    queued = query.all()

    results: List[PullResult] = []
    for row in queued:
        recorder_type = row.recorder_type
        recorder_ref = row.recorder_ref
        source = row.source or None
        try:
            res = pull_recorder_movements(
                session,
                recorder_type,
                recorder_ref,
                client=client,
                source=source,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 — isolate a bad recorder
            session.rollback()
            _upsert_pull_row(
                session,
                recorder_type,
                recorder_ref,
                status="error",
                source=source,
                bump_attempt=True,
                last_error=str(exc),
            )
            session.commit()
            res = PullResult(
                recorder_type=recorder_type,
                recorder_ref=recorder_ref,
                status="error",
                error=str(exc),
            )
        results.append(res)
    return results
