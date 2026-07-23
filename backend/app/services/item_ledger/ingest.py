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
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import models
from .physical import (
    LedgerKey,
    _dec,
    canonical_content_hash,
    canonical_decimal,
    ensure_physical_import_batch,
    rebuild_running_balance,
)

# Register + document-pull tags.
REGISTER_ENTITY = "AccumulationRegister_ЗапасыНаСкладах"
INGEST_SOURCE = "document_pull"
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
_MOSCOW = ZoneInfo("Europe/Moscow")

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


class HistoricalPullValidationError(ValueError):
    """A recorder cannot join a strict, cutoff-bounded historical prefix."""


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


def _coerce_period(value: Optional[datetime]) -> Optional[datetime]:
    """Return the Ledger's canonical naive Europe/Moscow timestamp."""
    if value is None:
        return None
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(_MOSCOW).replace(tzinfo=None)
    return value


def _movement_kind(recorder_type: str, record_type: str) -> str:
    is_receipt = record_type == "Receipt"
    if "Перемещение" in recorder_type:
        return "transfer_in" if is_receipt else "transfer_out"
    if "Сборка" in recorder_type:
        return "assembly_in" if is_receipt else "assembly_out"
    return "receipt" if is_receipt else "expense"


def _stable_lock_key(recorder_type: str, recorder_ref: str) -> int:
    """Deterministic signed 64-bit key for pg_advisory_xact_lock(bigint)."""
    identity = f"{recorder_type or ''}\x1f{recorder_ref or ''}"
    digest = hashlib.sha1(identity.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _advisory_xact_lock(
    session: Session, recorder_type: str, recorder_ref: str
) -> None:
    """Per-recorder advisory lock on PostgreSQL; no-op elsewhere (e.g. SQLite)."""
    bind = session.get_bind()
    try:
        dialect = bind.dialect.name
    except Exception:
        dialect = ""
    if dialect == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": _stable_lock_key(recorder_type, recorder_ref)},
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


def _latest_recorder_import_batch(
    session: Session,
    recorder_type: str,
    recorder_ref: str,
) -> Optional[models.PhysicalImportBatch]:
    """Latest committed semantic state for one recorder.

    The recorder advisory lock is held by the caller, so this boundary cannot
    change between the state comparison and insertion of the next revision.
    """
    return (
        session.query(models.PhysicalImportBatch)
        .filter(
            models.PhysicalImportBatch.source_watermarks[
                "recorder_type"
            ].as_string()
            == recorder_type,
            models.PhysicalImportBatch.source_watermarks[
                "recorder_ref"
            ].as_string()
            == recorder_ref,
        )
        .order_by(models.PhysicalImportBatch.id.desc())
        .first()
    )


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
    import_batch: Optional[models.PhysicalImportBatch] = None,
    ledger_generation_id: Optional[int] = None,
    max_posting_at: Optional[datetime] = None,
    strict_historical: bool = False,
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

    # The lock must cover the remote read as well as the local apply. Otherwise
    # two transactions may fetch different 1C revisions and apply them in the
    # reverse order after waiting only at the write boundary.
    _advisory_xact_lock(session, recorder_type, recorder_ref)

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
        parsed_period = _coerce_period(_parse_period(line.get("Period")))
        if parsed_period is None and (strict_historical or max_posting_at is not None):
            raise HistoricalPullValidationError(
                f"malformed Period in {recorder_type} {recorder_ref} "
                f"line {line.get('LineNumber')}: {line.get('Period')!r}"
            )
        if parsed_period is not None and max_posting_at is not None:
            cutoff_period = _coerce_period(max_posting_at)
            assert cutoff_period is not None
            beyond_cutoff = parsed_period > cutoff_period
            if beyond_cutoff:
                raise HistoricalPullValidationError(
                    f"recorder movement {parsed_period.isoformat()} exceeds "
                    f"historical cutoff {max_posting_at.isoformat()} in "
                    f"{recorder_type} {recorder_ref}"
                )
        if line.get("Active") is False:
            result.skipped_inactive += 1
            continue

        wh = _norm_ref(line.get("СтруктурнаяЕдиница_Key"))
        if wh not in warehouses:
            if strict_historical:
                raise HistoricalPullValidationError(
                    f"unknown warehouse ref {wh or '<empty>'} "
                    f"(line {line.get('LineNumber')}) in "
                    f"{recorder_type} {recorder_ref}"
                )
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
            if strict_historical:
                raise HistoricalPullValidationError(
                    f"unknown item ref {item_ref or '<empty>'} "
                    f"(line {line.get('LineNumber')}) in "
                    f"{recorder_type} {recorder_ref}"
                )
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
        posting_at = parsed_period or datetime.now()

        key = LedgerKey(int(item_id), char, org, wh)

        t0 = _anchor_t0(session, key, anchor_cache)
        if t0 is not None and posting_at <= t0:
            result.skipped_pre_anchor += 1
            continue

        normalized.append((key, signed_qty, record_type, line_no, posting_at))

    normalized.sort(
        key=lambda row: (
            row[3],
            row[4].isoformat(),
            tuple(row[0]),
            str(row[1]),
            row[2],
        )
    )
    normalized_payload = [
        {
            "item_id": key.item_id,
            "characteristic_ref": key.characteristic_ref,
            "organization_ref": key.organization_ref,
            "warehouse_ref1c": key.warehouse_ref1c,
            "qty": canonical_decimal(signed_qty),
            "record_type": record_type,
            "line_no": line_no,
            "posting_at": posting_at.isoformat(),
        }
        for key, signed_qty, record_type, line_no, posting_at in normalized
    ]
    content_hash = canonical_content_hash(normalized_payload)

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
            models.StockLedgerEntry.active.is_(True),
        )
        .distinct()
        .all()
    )
    touched: Dict[LedgerKey, None] = {
        LedgerKey(int(r[0]), r[1] or "", r[2] or "", r[3] or ""): None
        for r in existing_key_rows
    }

    active_rows = (
        session.query(models.StockLedgerEntry)
        .filter(
            models.StockLedgerEntry.recorder_type == recorder_type,
            models.StockLedgerEntry.recorder_ref == recorder_ref,
            models.StockLedgerEntry.ingest_source == INGEST_SOURCE,
            models.StockLedgerEntry.active.is_(True),
        )
        .all()
    )
    existing_hashes = {str(row.source_content_hash) for row in active_rows}
    latest_batch = _latest_recorder_import_batch(
        session, recorder_type, recorder_ref
    )
    latest_watermark = (
        dict(latest_batch.source_watermarks or {})
        if latest_batch is not None
        else {}
    )
    expected_line_count = len(normalized)
    active_state_matches = (
        len(active_rows) == expected_line_count
        and (
            (expected_line_count == 0 and not existing_hashes)
            or existing_hashes == {content_hash}
        )
    )

    # Exact re-pull: a true no-op. Preserve SLE ids and all event provenance.
    # The batch watermark is essential for the empty-document state, which has
    # no active SLE row of its own.
    if (
        latest_batch is not None
        and latest_watermark.get("content_hash") == content_hash
        and int(latest_watermark.get("line_count", -1)) == expected_line_count
        and active_state_matches
    ):
        result.status = "done" if expected_line_count else "empty"
        pull_row = _upsert_pull_row(
            session,
            recorder_type,
            recorder_ref,
            status=result.status,
            line_count=expected_line_count,
            source=source,
            last_error=None,
        )
        if header_order_ref:
            pull_row.order_ref = header_order_ref
        result.touched_keys = []
        session.flush()
        return result

    import_batch = ensure_physical_import_batch(
        session,
        batch_key=(
            f"recorder:{canonical_content_hash([recorder_type, recorder_ref])[:24]}:"
            f"after:{int(latest_batch.id) if latest_batch is not None else 0}:"
            f"{content_hash[:32]}"
        ),
        cutoff=max((row[4] for row in normalized), default=datetime.now()),
        source_watermarks={
            "source": REGISTER_ENTITY,
            "recorder_type": recorder_type,
            "recorder_ref": recorder_ref,
            "content_hash": content_hash,
            "line_count": expected_line_count,
            "previous_import_batch_id": (
                int(latest_batch.id) if latest_batch is not None else None
            ),
        },
        batch=import_batch,
    )

    replaced_sle_ids = [int(row.id) for row in active_rows]
    if replaced_sle_ids and ledger_generation_id is not None:
        from .reservation_ledger import unrealize_replaced_sle

        provenance = {
            int(event.id): int(event.sle_id)
            for event in session.query(models.ReservationEvent)
            .filter(models.ReservationEvent.sle_id.in_(replaced_sle_ids))
            .all()
            if event.sle_id is not None
        }
        unrealize_replaced_sle(
            session,
            replaced_sle_ids,
            recorder_ref,
            ledger_generation_id=ledger_generation_id,
        )
        if provenance:
            for event in session.query(models.ReservationEvent).filter(
                models.ReservationEvent.id.in_(list(provenance))
            ):
                event.sle_id = provenance[int(event.id)]

    for old in active_rows:
        old.active = False

    new_by_line: Dict[str, models.StockLedgerEntry] = {}
    for key, signed_qty, record_type, line_no, posting_at in normalized:
        entry = models.StockLedgerEntry(
                ingest_batch_id=import_batch.id,
                source_content_hash=content_hash,
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
        session.add(entry)
        new_by_line[line_no] = entry
        touched[key] = None
    session.flush()

    for old in active_rows:
        new = new_by_line.get(str(old.line_no or ""))
        session.add(
            models.StockLedgerFactSupersession(
                old_sle_id=old.id,
                new_sle_id=new.id if new is not None else None,
                import_batch_id=import_batch.id,
            )
        )

    result.inserted = len(normalized)
    for key in touched:
        rebuild_running_balance(
            session, key, ledger_generation_id=ledger_generation_id
        )
    result.touched_keys = list(touched.keys())

    # Trigger т1 (design §5): event-driven incremental redistribute of the pools
    # touched by this pull — realize/unrealize the fresh facts, THEN refresh the
    # coverage caches, so uncovered / position стая current between full ledger
    # cycles. Guarded internally: a failure logs and never breaks the pull (the
    # cycle re-materializes the caches). The replace-by-recorder unrealize above
    # already compensated the deleted rows; this re-matches the fresh ones.
    touched_item_ids = {k.item_id for k in touched}
    if touched_item_ids and ledger_generation_id is not None:
        from .reservation_ledger import redistribute_after_ledger_apply

        redistribute_after_ledger_apply(
            session,
            touched_item_ids,
            f"pull:{recorder_ref}"[:64],
            ledger_generation_id=ledger_generation_id,
        )

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
    ledger_generation_id: Optional[int] = None,
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
                ledger_generation_id=ledger_generation_id,
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
