"""Read-only historical scanner for the 1C warehouse movement register.

The scanner discovers recorder identities only. It does not pull recorder
contents, write Ledger rows, or advance a database watermark. Progress is
exposed exclusively at completed, non-overlapping window boundaries.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence


REGISTER_RECORD_ENTITY = "AccumulationRegister_ЗапасыНаСкладах_RecordType"
REGISTER_ORDER_BY = "Period,Recorder_Type,Recorder,LineNumber"
REGISTER_SELECT_FIELDS = ("Period", "Recorder", "Recorder_Type", "LineNumber")


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
    return value.replace(microsecond=0).isoformat()


def _strict_period(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        raise HistoricalRegisterScanError("register row has no Period")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HistoricalRegisterScanError(
            f"register row has malformed Period: {raw!r}"
        ) from exc


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
    return raw.strip("{}").strip()


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
) -> tuple[WindowCheckpoint, dict[RecorderIdentity, datetime]]:
    filter_query = (
        f"Period gt datetime'{_odata_datetime(from_exclusive)}' and "
        f"Period le datetime'{_odata_datetime(to_inclusive)}'"
    )
    offset = 0
    pages_read = 0
    rows_read = 0
    seen_page_hashes: set[str] = set()
    recorder_periods: dict[RecorderIdentity, datetime] = {}
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
            period = _strict_period(row.get("Period"))
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
            previous = recorder_periods.get(identity)
            if previous is None or period < previous:
                recorder_periods[identity] = period

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
            recorder_count=len(recorder_periods),
            content_hash=content_hash,
        ),
        recorder_periods,
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
    recorder_periods: dict[RecorderIdentity, datetime] = {}
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
        for identity, period in discovered.items():
            previous = recorder_periods.get(identity)
            if previous is None or period < previous:
                recorder_periods[identity] = period
        cursor = window_end

    recorders = tuple(
        DiscoveredRecorder(identity, period)
        for identity, period in sorted(
            recorder_periods.items(),
            key=lambda pair: (
                pair[1],
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
