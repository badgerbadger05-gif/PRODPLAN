from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from app.services.item_ledger.historical_register_scan import (
    HistoricalRegisterScanError,
    REGISTER_ORDER_BY,
    REGISTER_RECORD_ENTITY,
    scan_historical_register_range,
)


def _row(period: datetime | str | None, recorder: str, line: int):
    return {
        "Period": period.isoformat() if isinstance(period, datetime) else period,
        "Recorder": recorder,
        "Recorder_Type": "StandardODATA.Document_СборкаЗапасов",
        "LineNumber": line,
        "Active": True,
        "RecordType": "Receipt",
        "Номенклатура_Key": "item-ref",
        "Характеристика_Key": "",
        "Организация_Key": "org-ref",
        "СтруктурнаяЕдиница_Key": "warehouse-ref",
        "Количество": 1,
    }


class FakeRangeClient:
    def __init__(self, rows, *, repeat_first_page=False):
        self.rows = list(rows)
        self.repeat_first_page = repeat_first_page
        self.calls = []

    def _make_request(self, entity, params):
        self.calls.append((entity, dict(params)))
        top = int(params["$top"])
        skip = 0 if self.repeat_first_page else int(params["$skip"])
        match = re.fullmatch(
            r"Period gt datetime'([^']+)' and Period le datetime'([^']+)'",
            params["$filter"],
        )
        assert match
        lower = datetime.fromisoformat(match.group(1))
        upper = datetime.fromisoformat(match.group(2))
        selected = [
            row for row in self.rows
            if lower < datetime.fromisoformat(str(row["Period"])) <= upper
        ]
        selected.sort(
            key=lambda row: (
                row["Period"],
                row["Recorder_Type"],
                row["Recorder"],
                row["LineNumber"],
            )
        )
        return {"value": selected[skip : skip + top]}

    def post(self, *args, **kwargs):
        raise AssertionError("historical scanner attempted an OData write")

    def patch(self, *args, **kwargs):
        raise AssertionError("historical scanner attempted an OData write")


def test_scans_more_than_1000_rows_with_period_ties_and_dedupes_recorders():
    start = datetime(2026, 1, 1)
    tied = start + timedelta(hours=1)
    rows = [
        _row(tied, f"rec-{index // 2:04d}", index)
        for index in range(2_505)
    ]
    client = FakeRangeClient(rows)

    result = scan_historical_register_range(
        client,
        from_exclusive=start,
        to_inclusive=start + timedelta(days=1),
        page_size=1000,
    )

    assert result.rows_read == 2_505
    assert len(result.recorders) == 1_253
    assert [call[1]["$skip"] for call in client.calls] == [0, 1000, 2000]
    assert all(call[0] == REGISTER_RECORD_ENTITY for call in client.calls)
    assert all(call[1]["$orderby"] == REGISTER_ORDER_BY for call in client.calls)
    assert result.recorders[0].identity.recorder_ref == "rec-0000"
    assert result.recorders[-1].identity.recorder_ref == "rec-1252"
    assert result.recorders[0].row_count == 2
    assert result.recorders[-1].row_count == 1
    assert result.recorders[0].balance_content_hash


def test_filter_dates_are_converted_to_naive_moscow_time():
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 2, tzinfo=timezone.utc)

    class CaptureFilterClient(FakeRangeClient):
        def __init__(self):
            super().__init__([])
            self.filters = []

        def _make_request(self, entity, params):
            self.filters.append(params["$filter"])
            return {"value": []}

    client = CaptureFilterClient()
    scan_historical_register_range(
        client,
        from_exclusive=start,
        to_inclusive=end,
        window_size=timedelta(days=1),
    )

    assert len(client.filters) == 1
    assert client.filters[0] == (
        "Period gt datetime'2026-06-01T03:00:00' and "
        "Period le datetime'2026-06-02T03:00:00'"
    )


def test_scanner_treats_naive_1c_period_as_moscow_for_aware_utc_window():
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 2, tzinfo=timezone.utc)
    row = _row(datetime(2026, 6, 1, 15, 0, 0), "rec-1", 1)  # naive Moscow from 1C
    result = scan_historical_register_range(
        FakeRangeClient([row]),
        from_exclusive=start,
        to_inclusive=end,
        window_size=timedelta(days=1),
    )

    assert result.rows_read == 1
    assert result.recorders[0].identity.recorder_ref == "rec-1"
    assert (
        result.recorders[0].first_period
        == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    )


def test_resume_starts_at_last_completed_window_and_rereads_unfinished_from_zero():
    start = datetime(2026, 1, 1)
    rows = [
        _row(start + timedelta(hours=12), "old", 1),
        _row(start + timedelta(days=1, hours=12), "unfinished", 1),
        _row(start + timedelta(days=2, hours=12), "last", 1),
    ]
    client = FakeRangeClient(rows)

    result = scan_historical_register_range(
        client,
        from_exclusive=start,
        to_inclusive=start + timedelta(days=3),
        completed_through=start + timedelta(days=1),
        window_size=timedelta(days=1),
    )

    assert [row.identity.recorder_ref for row in result.recorders] == [
        "unfinished", "last",
    ]
    assert [window.from_exclusive for window in result.windows] == [
        start + timedelta(days=1),
        start + timedelta(days=2),
    ]
    assert all(call[1]["$skip"] == 0 for call in client.calls)


def test_recorder_seen_in_multiple_windows_is_returned_once_at_first_period():
    start = datetime(2026, 1, 1)
    first = start + timedelta(hours=12)
    rows = [
        _row(first, "same-recorder", 1),
        _row(start + timedelta(days=1, hours=12), "same-recorder", 2),
    ]

    result = scan_historical_register_range(
        FakeRangeClient(rows),
        from_exclusive=start,
        to_inclusive=start + timedelta(days=2),
        window_size=timedelta(days=1),
    )

    assert len(result.recorders) == 1
    assert result.recorders[0].identity.recorder_ref == "same-recorder"
    assert result.recorders[0].first_period == first


def test_windows_are_non_overlapping_and_bounds_are_from_exclusive_to_inclusive():
    start = datetime(2026, 1, 1)
    boundary = start + timedelta(days=1)
    rows = [
        _row(start, "excluded-start", 1),
        _row(boundary, "included-once", 1),
        _row(boundary + timedelta(seconds=1), "second-window", 1),
        _row(start + timedelta(days=2, seconds=1), "past-cutoff", 1),
    ]
    client = FakeRangeClient(rows)

    result = scan_historical_register_range(
        client,
        from_exclusive=start,
        to_inclusive=start + timedelta(days=2),
        window_size=timedelta(days=1),
    )

    assert [row.identity.recorder_ref for row in result.recorders] == [
        "included-once", "second-window",
    ]
    assert [window.rows_read for window in result.windows] == [1, 1]


def test_resume_rejects_partial_window_watermark():
    start = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="window boundary"):
        scan_historical_register_range(
            FakeRangeClient([]),
            from_exclusive=start,
            to_inclusive=start + timedelta(days=2),
            completed_through=start + timedelta(hours=12),
            window_size=timedelta(days=1),
        )


@pytest.mark.parametrize("period", [None, "", "not-a-date"])
def test_rejects_missing_or_malformed_period(period):
    start = datetime(2026, 1, 1)

    class MalformedClient:
        def _make_request(self, entity, params):
            return {"value": [_row(period, "bad", 1)]}

    with pytest.raises(HistoricalRegisterScanError, match="Period"):
        scan_historical_register_range(
            MalformedClient(),
            from_exclusive=start,
            to_inclusive=start + timedelta(days=1),
        )


def test_repeated_page_is_rejected_instead_of_advancing_checkpoint():
    start = datetime(2026, 1, 1)
    rows = [_row(start + timedelta(hours=1), f"rec-{i}", i) for i in range(3)]
    client = FakeRangeClient(rows, repeat_first_page=True)

    with pytest.raises(HistoricalRegisterScanError, match="repeated page"):
        scan_historical_register_range(
            client,
            from_exclusive=start,
            to_inclusive=start + timedelta(days=1),
            page_size=2,
        )


def test_scanner_uses_only_read_request_method():
    start = datetime(2026, 1, 1)
    client = FakeRangeClient([_row(start + timedelta(seconds=1), "rec", 1)])

    result = scan_historical_register_range(
        client,
        from_exclusive=start,
        to_inclusive=start + timedelta(days=1),
    )

    assert len(result.recorders) == 1
    assert len(client.calls) == 1


def test_balance_hash_changes_when_same_count_row_changes_warehouse():
    start = datetime(2026, 1, 1)
    period = start + timedelta(hours=1)
    original = _row(period, "same-recorder", 1)
    changed = dict(original)
    changed["СтруктурнаяЕдиница_Key"] = "other-warehouse"

    original_scan = scan_historical_register_range(
        FakeRangeClient([original]),
        from_exclusive=start,
        to_inclusive=start + timedelta(days=1),
    )
    changed_scan = scan_historical_register_range(
        FakeRangeClient([changed]),
        from_exclusive=start,
        to_inclusive=start + timedelta(days=1),
    )

    assert original_scan.recorders[0].row_count == 1
    assert changed_scan.recorders[0].row_count == 1
    assert (
        original_scan.recorders[0].balance_content_hash
        != changed_scan.recorders[0].balance_content_hash
    )
