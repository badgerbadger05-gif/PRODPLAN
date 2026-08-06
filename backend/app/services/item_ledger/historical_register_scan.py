"""Read-only historical scanner for the 1C warehouse movement register.

The scanner discovers recorder identities plus a compact hash of the
balance-relevant row fields already present in the flat register. It does not
pull document contents, write Ledger rows, or advance a database watermark.
Progress is exposed exclusively at completed, non-overlapping window
boundaries.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from typing import Any, Mapping, Sequence


REGISTER_RECORD_ENTITY = "AccumulationRegister_ЗапасыНаСкладах_RecordType"
REGISTER_ORDER_BY = "Period,Recorder_Type,Recorder,LineNumber"
REGISTER_SELECT_FIELDS = (
    "Period",
    "Recorder",
    "Recorder_Type",
    "LineNumber",
    "Active",
    "RecordType",
    "Номенклатура_Key",
    "Характеристика_Key",
    "Организация_Key",
    "СтруктурнаяЕдиница_Key",
    "Количество",
)
_MOSCOW = ZoneInfo("Europe/Moscow")


class HistoricalRegisterScanError(ValueError):
    """The register response cannot form a deterministic historical prefix."""


@dataclass(frozen=True, order=True)
class RecorderIdentity:
    recorder_type: str
    recorder_ref: str


@dataclass(frozen=True)
class DiscoveredRecorder:
    identity: RecorderIdentity
    first_period: datetime
    row_count: int
    balance_content_hash: str


@dataclass(frozen=True)
class WindowCheckpoint:
    from_exclusive: datetime
    to_inclusive: datetime
    pages_read: int
    rows_read: int
    recorder_count: int
    content_hash: str


@dataclass(frozen=True)
class RegisterRangeScanResult:
    from_exclusive: datetime
    to_inclusive: datetime
    resumed_after: datetime
    completed_through: datetime
    windows: tuple[WindowCheckpoint, ...]
    recorders: tuple[DiscoveredRecorder, ...]
    rows_read: int


def _odata_datetime(value: datetime) -> str:
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(ZoneInfo("Europe/Moscow"))
    return value.replace(tzinfo=None, microsecond=0).isoformat()


def _strict_period(value: Any, *, range_anchor: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise HistoricalRegisterScanError("register row has no Period")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise HistoricalRegisterScanError(
                f"register row has malformed Period: {raw!r}"
            ) from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # 1C historical register returns naive datetimes for registry rows.
        # Treat them as Europe/Moscow local values (aligned with historical read
        # path and acceptance windows).
        parsed = parsed.replace(tzinfo=_MOSCOW)
    elif range_anchor.tzinfo is not None and parsed.tzinfo is not None:
        parsed = parsed.astimezone(range_anchor.tzinfo)

    if range_anchor.tzinfo is None or range_anchor.utcoffset() is None:
        # Keep backward-compatible naive behaviour for fully naive callers.
        return parsed.replace(tzinfo=None)
    return parsed


def _normalize_recorder_type(value: Any) -> str:
    raw = str(value or "").strip()
    prefix = "StandardODATA."
    return raw[len(prefix) :] if raw.startswith(prefix) else raw


def _normalize_recorder_ref(value: Any) -> str:
    if isinstance(value, Mapping):
        value = (
            value.get("Ref_Key")
            or value.get("RefKey")
            or value.get("ref_key")
        )
    raw = str(value or "").strip()
    if raw.startswith("guid'") and raw.endswith("'"):
        raw = raw[5:-1]
    normalized = raw.strip("{}").strip()
    return "" if normalized == "00000000-0000-0000-0000-000000000000" else normalized


def _canonical_decimal(value: Any) -> str:
    normalized = Decimal(str(value or 0)).normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def balance_movement_payload(
    *,
    item_ref: Any,
    characteristic_ref: Any,
    organization_ref: Any,
    warehouse_ref: Any,
    signed_qty: Any,
    record_type: Any,
    line_no: Any,
) -> dict[str, str]:
    """Canonical balance-relevant movement fields shared with accepted Ledger."""
    return {
        "item_ref": _normalize_recorder_ref(item_ref),
        "characteristic_ref": _normalize_recorder_ref(characteristic_ref),
        "organization_ref": _normalize_recorder_ref(organization_ref),
        "warehouse_ref": _normalize_recorder_ref(warehouse_ref),
        "qty": _canonical_decimal(signed_qty),
        "record_type": str(record_type or ""),
        "line_no": str(line_no or ""),
    }


def balance_content_hash(payloads: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(
        (dict(payload) for payload in payloads),
        key=lambda payload: json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return hashlib.sha256(
        json.dumps(
            ordered,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _register_movement_payload(row: Mapping[str, Any]) -> dict[str, str] | None:
    if row.get("Active") is False:
        return None
    quantity = Decimal(str(row.get("Количество") or 0))
    if quantity == 0:
        return None
    record_type = str(row.get("RecordType") or "")
    signed_qty = -quantity if record_type == "Expense" else quantity
    return balance_movement_payload(
        item_ref=row.get("Номенклатура_Key"),
        characteristic_ref=row.get("Характеристика_Key"),
        organization_ref=row.get("Организация_Key"),
        warehouse_ref=row.get("СтруктурнаяЕдиница_Key"),
        signed_qty=signed_qty,
        record_type=record_type,
        line_no=row.get("LineNumber"),
    )


def _page_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(rows),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scan_window(
    client: Any,
    *,
    from_exclusive: datetime,
    to_inclusive: datetime,
    page_size: int,
    max_pages: int,
) -> tuple[
    WindowCheckpoint,
    dict[RecorderIdentity, tuple[datetime, list[dict[str, str]]]],
]:
    filter_query = (
        f"Period gt datetime'{_odata_datetime(from_exclusive)}' and "
        f"Period le datetime'{_odata_datetime(to_inclusive)}'"
    )
    offset = 0
    pages_read = 0
    rows_read = 0
    seen_page_hashes: set[str] = set()
    recorder_states: dict[
        RecorderIdentity,
        tuple[datetime, list[dict[str, str]]],
    ] = {}
    row_hashes: list[str] = []

    while True:
        if pages_read >= max_pages:
            raise HistoricalRegisterScanError(
                f"register window exceeded max_pages={max_pages}"
            )
        params = {
            "$top": page_size,
            "$skip": offset,
            "$filter": filter_query,
            "$select": ",".join(REGISTER_SELECT_FIELDS),
            "$orderby": REGISTER_ORDER_BY,
        }
        response = client._make_request(REGISTER_RECORD_ENTITY, params)
        if not isinstance(response, Mapping) or not isinstance(
            response.get("value"), list
        ):
            raise HistoricalRegisterScanError(
                "register page response must contain a list in 'value'"
            )
        rows = response["value"]
        if not rows:
            break
        if not all(isinstance(row, Mapping) for row in rows):
            raise HistoricalRegisterScanError("register page contains a non-object row")

        digest = _page_hash(rows)
        if digest in seen_page_hashes:
            raise HistoricalRegisterScanError(
                f"register repeated page at offset {offset}"
            )
        seen_page_hashes.add(digest)
        row_hashes.append(digest)
        pages_read += 1

        for row in rows:
            period = _strict_period(row.get("Period"), range_anchor=from_exclusive)
            try:
                in_bounds = from_exclusive < period <= to_inclusive
            except TypeError as exc:
                raise HistoricalRegisterScanError(
                    "register Period timezone does not match range timezone"
                ) from exc
            if not in_bounds:
                raise HistoricalRegisterScanError(
                    f"register returned Period outside ({from_exclusive}, {to_inclusive}]: "
                    f"{period}"
                )
            identity = RecorderIdentity(
                _normalize_recorder_type(row.get("Recorder_Type")),
                _normalize_recorder_ref(row.get("Recorder")),
            )
            if not identity.recorder_type or not identity.recorder_ref:
                raise HistoricalRegisterScanError(
                    "register row has incomplete Recorder identity"
                )
            payload = _register_movement_payload(row)
            if payload is None:
                continue
            previous = recorder_states.get(identity)
            recorder_states[identity] = (
                period if previous is None else min(previous[0], period),
                [payload] if previous is None else [*previous[1], payload],
            )

        rows_read += len(rows)
        if len(rows) < page_size:
            break
        offset += len(rows)

    content_hash = hashlib.sha256("|".join(row_hashes).encode("ascii")).hexdigest()
    return (
        WindowCheckpoint(
            from_exclusive=from_exclusive,
            to_inclusive=to_inclusive,
            pages_read=pages_read,
            rows_read=rows_read,
            recorder_count=len(recorder_states),
            content_hash=content_hash,
        ),
        recorder_states,
    )


def scan_historical_register_range(
    client: Any,
    *,
    from_exclusive: datetime,
    to_inclusive: datetime,
    window_size: timedelta = timedelta(days=1),
    completed_through: datetime | None = None,
    page_size: int = 1000,
    max_pages_per_window: int = 10_000,
) -> RegisterRangeScanResult:
    """Discover recorders in ``(from_exclusive, to_inclusive]``.

    ``completed_through`` may only name a previously completed window boundary.
    No partial-page cursor is accepted: after a crash the caller supplies the
    last durable boundary and this function rereads the whole unfinished
    window from offset zero.
    """
    if to_inclusive <= from_exclusive:
        raise ValueError("to_inclusive must be greater than from_exclusive")
    if window_size <= timedelta(0):
        raise ValueError("window_size must be positive")
    if page_size <= 0 or max_pages_per_window <= 0:
        raise ValueError("page_size and max_pages_per_window must be positive")

    resume = completed_through or from_exclusive
    if resume < from_exclusive or resume > to_inclusive:
        raise ValueError("completed_through is outside the requested range")
    if resume != to_inclusive and (resume - from_exclusive) % window_size:
        raise ValueError("completed_through must be a completed window boundary")

    cursor = resume
    windows: list[WindowCheckpoint] = []
    recorder_states: dict[
        RecorderIdentity,
        tuple[datetime, list[dict[str, str]]],
    ] = {}
    while cursor < to_inclusive:
        window_end = min(cursor + window_size, to_inclusive)
        checkpoint, discovered = _scan_window(
            client,
            from_exclusive=cursor,
            to_inclusive=window_end,
            page_size=page_size,
            max_pages=max_pages_per_window,
        )
        windows.append(checkpoint)
        for identity, (period, payloads) in discovered.items():
            previous = recorder_states.get(identity)
            recorder_states[identity] = (
                period if previous is None else min(previous[0], period),
                payloads if previous is None else [*previous[1], *payloads],
            )
        cursor = window_end

    recorders = tuple(
        DiscoveredRecorder(
            identity,
            state[0],
            len(state[1]),
            balance_content_hash(state[1]),
        )
        for identity, state in sorted(
            recorder_states.items(),
            key=lambda pair: (
                pair[1][0],
                pair[0].recorder_type,
                pair[0].recorder_ref,
            ),
        )
    )
    return RegisterRangeScanResult(
        from_exclusive=from_exclusive,
        to_inclusive=to_inclusive,
        resumed_after=resume,
        completed_through=cursor,
        windows=tuple(windows),
        recorders=recorders,
        rows_read=sum(window.rows_read for window in windows),
    )
