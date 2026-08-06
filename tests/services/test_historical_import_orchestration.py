from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from app import models
from app.services.item_ledger.historical_import_orchestration import (
    HistoricalImportError,
    run_historical_physical_import,
)


RECORDER_TYPE = "Document_СборкаЗапасов"


def _register_row(period: datetime, recorder_ref: str):
    return {
        "Period": period.isoformat(),
        "Recorder": recorder_ref,
        "Recorder_Type": f"StandardODATA.{RECORDER_TYPE}",
        "LineNumber": 1,
        "Active": True,
        "RecordType": "Receipt",
        "Номенклатура_Key": "ITEM-REF",
        "СтруктурнаяЕдиница_Key": "WH-REF",
        "Характеристика_Key": "",
        "Организация_Key": "",
        "Количество": 1,
    }


def _movement(
    period,
    *,
    item_ref="ITEM-REF",
    warehouse_ref="WH-REF",
):
    return {
        "Active": True,
        "RecordType": "Receipt",
        "Номенклатура_Key": item_ref,
        "СтруктурнаяЕдиница_Key": warehouse_ref,
        "Характеристика_Key": "",
        "Организация_Key": "",
        "Количество": 1,
        "LineNumber": 1,
        "Period": period.isoformat() if isinstance(period, datetime) else period,
    }


class HistoricalClient:
    def __init__(self, register_rows, movements, *, fail_once=None):
        self.register_rows = list(register_rows)
        self.movements = dict(movements)
        self.fail_once = set(fail_once or ())
        self.read_calls = []
        self.write_calls = []

    def _make_request(self, entity, params):
        self.read_calls.append(("_make_request", entity, dict(params)))
        match = re.fullmatch(
            r"Period gt datetime'([^']+)' and Period le datetime'([^']+)'",
            params["$filter"],
        )
        assert match
        lower = datetime.fromisoformat(match.group(1))
        upper = datetime.fromisoformat(match.group(2))
        rows = [
            row for row in self.register_rows
            if lower < datetime.fromisoformat(row["Period"]) <= upper
        ]
        rows.sort(key=lambda row: (
            row["Period"], row["Recorder_Type"], row["Recorder"], row["LineNumber"],
        ))
        skip = int(params["$skip"])
        top = int(params["$top"])
        return {"value": rows[skip : skip + top]}

    def get_all(self, entity_name, *, filter_query=None, **_kwargs):
        self.read_calls.append(("get_all", entity_name, filter_query))
        if entity_name == "AccumulationRegister_ЗапасыНаСкладах":
            match = re.search(r"guid'([^']+)'", str(filter_query))
            recorder_ref = match.group(1)
            if recorder_ref in self.fail_once:
                self.fail_once.remove(recorder_ref)
                raise RuntimeError(f"simulated crash pulling {recorder_ref}")
            return [{"RecordSet": list(self.movements.get(recorder_ref, []))}]
        # Optional document-header lookup is read-only and may be empty.
        return []

    def post(self, *args, **kwargs):
        self.write_calls.append(("post", args, kwargs))
        raise AssertionError("historical import attempted a 1C write")

    def patch(self, *args, **kwargs):
        self.write_calls.append(("patch", args, kwargs))
        raise AssertionError("historical import attempted a 1C write")


def _generation(db, *, cutoff):
    item = models.Item(
        item_code="HIST-ITEM",
        item_name="Historical item",
        item_ref1c="ITEM-REF",
    )
    warehouse = models.StockWarehouse(
        warehouse_ref1c="WH-REF",
        warehouse_name="Historical warehouse",
    )
    baseline = models.PhysicalImportBatch(
        batch_key="historical-baseline",
        status="completed",
        cutoff=cutoff - timedelta(days=10),
        source_watermarks={},
        completed_at=cutoff - timedelta(days=10),
    )
    generation = models.LedgerGeneration(
        generation_key="historical-building",
        status="building",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=baseline,
        algorithm_version="tests/historical",
    )
    db.add_all([item, warehouse, generation])
    db.commit()
    return generation, baseline


def test_crash_preserves_completed_window_and_resume_is_idempotent(db_session):
    start = datetime(2026, 1, 1)
    cutoff = start + timedelta(days=2)
    generation, baseline = _generation(db_session, cutoff=cutoff)
    first_period = start + timedelta(hours=1)
    second_period = start + timedelta(days=1, hours=1)
    client = HistoricalClient(
        [
            _register_row(first_period, "REC-1"),
            _register_row(second_period, "REC-2"),
        ],
        {
            "REC-1": [_movement(first_period)],
            "REC-2": [_movement(second_period)],
        },
        fail_once={"REC-2"},
    )

    with pytest.raises(HistoricalImportError, match="REC-2"):
        run_historical_physical_import(
            db_session,
            ledger_generation_id=generation.id,
            client=client,
            from_exclusive=start,
            to_inclusive=cutoff,
        )

    assert db_session.query(models.LedgerBuildBatch).count() == 1
    assert db_session.query(models.StockLedgerEntry).count() == 1
    first_checkpoint = db_session.query(models.LedgerBuildBatch).one()
    recorder_metrics = first_checkpoint.metrics["recorders"]
    assert recorder_metrics[0]["recorder_type"] == RECORDER_TYPE
    assert recorder_metrics[0]["recorder_ref"] == "REC-1"
    assert len(recorder_metrics[0]["state_checksum"]) == 64
    assert first_checkpoint.metrics["pull_checksum"]
    db_session.refresh(generation)
    first_terminal_id = generation.physical_import_batch_id
    assert first_terminal_id != baseline.id
    assert db_session.get(models.PhysicalImportBatch, first_terminal_id).status == "completed"

    resumed = run_historical_physical_import(
        db_session,
        ledger_generation_id=generation.id,
        client=client,
        from_exclusive=start,
        to_inclusive=cutoff,
    )
    assert resumed.complete is True
    assert resumed.windows_resumed == 1
    assert resumed.windows_completed == 1
    assert db_session.query(models.LedgerBuildBatch).count() == 2
    assert db_session.query(models.StockLedgerEntry).count() == 2

    repeated = run_historical_physical_import(
        db_session,
        ledger_generation_id=generation.id,
        client=client,
        from_exclusive=start,
        to_inclusive=cutoff,
    )
    assert repeated.windows_resumed == 2
    assert repeated.windows_completed == 0
    assert db_session.query(models.StockLedgerEntry).count() == 2
    assert client.write_calls == []


def test_max_windows_yields_only_at_completed_boundary(db_session):
    start = datetime(2026, 2, 1)
    cutoff = start + timedelta(days=2)
    generation, _baseline = _generation(db_session, cutoff=cutoff)
    client = HistoricalClient([], {})

    first = run_historical_physical_import(
        db_session,
        ledger_generation_id=generation.id,
        client=client,
        from_exclusive=start,
        to_inclusive=cutoff,
        max_windows=1,
    )

    assert first.complete is False
    assert first.completed_through == start + timedelta(days=1)
    assert first.windows_completed == 1
    assert db_session.query(models.LedgerBuildBatch).count() == 1

    second = run_historical_physical_import(
        db_session,
        ledger_generation_id=generation.id,
        client=client,
        from_exclusive=start,
        to_inclusive=cutoff,
    )
    assert second.complete is True
    assert second.windows_resumed == 1
    assert second.windows_completed == 1


def test_external_physical_batch_between_windows_blocks_resume(db_session):
    start = datetime(2026, 2, 10)
    cutoff = start + timedelta(days=2)
    generation, _baseline = _generation(db_session, cutoff=cutoff)
    client = HistoricalClient([], {})
    first = run_historical_physical_import(
        db_session,
        ledger_generation_id=generation.id,
        client=client,
        from_exclusive=start,
        to_inclusive=cutoff,
        max_windows=1,
    )
    generation_terminal = first.physical_import_batch_id
    external = models.PhysicalImportBatch(
        batch_key="unrelated-external-import",
        status="completed",
        cutoff=cutoff,
        source_watermarks={"source": "unrelated"},
        completed_at=cutoff,
    )
    db_session.add(external)
    db_session.commit()
    assert external.id > generation_terminal

    with pytest.raises(HistoricalImportError, match="sequence interleaved"):
        run_historical_physical_import(
            db_session,
            ledger_generation_id=generation.id,
            client=client,
            from_exclusive=start,
            to_inclusive=cutoff,
        )

    db_session.refresh(generation)
    assert generation.physical_import_batch_id == generation_terminal
    assert db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=generation.id,
    ).count() == 1


def test_recorder_movement_after_fixed_cutoff_never_advances_checkpoint(db_session):
    start = datetime(2026, 3, 1)
    cutoff = start + timedelta(days=1)
    generation, baseline = _generation(db_session, cutoff=cutoff)
    discovered = start + timedelta(hours=1)
    client = HistoricalClient(
        [_register_row(discovered, "REC-LATE")],
        {"REC-LATE": [_movement(cutoff + timedelta(seconds=1))]},
    )

    with pytest.raises(HistoricalImportError, match="exceeds historical cutoff"):
        run_historical_physical_import(
            db_session,
            ledger_generation_id=generation.id,
            client=client,
            from_exclusive=start,
            to_inclusive=cutoff,
        )

    db_session.refresh(generation)
    assert generation.physical_import_batch_id == baseline.id
    assert db_session.query(models.LedgerBuildBatch).count() == 0
    assert db_session.query(models.StockLedgerEntry).count() == 0


@pytest.mark.parametrize(
    ("movement", "message"),
    [
        (_movement(datetime(2026, 4, 1, 1), item_ref="UNKNOWN"), "unknown item"),
        (_movement(datetime(2026, 4, 1, 1), warehouse_ref="UNKNOWN"), "unknown warehouse"),
        (_movement("not-a-period"), "malformed Period"),
    ],
)
def test_strict_recorder_validation_blocks_terminal_boundary(
    db_session,
    movement,
    message,
):
    start = datetime(2026, 4, 1)
    cutoff = start + timedelta(days=1)
    generation, baseline = _generation(db_session, cutoff=cutoff)
    discovered = start + timedelta(minutes=1)
    client = HistoricalClient(
        [_register_row(discovered, "REC-BAD")],
        {"REC-BAD": [movement]},
    )

    with pytest.raises(HistoricalImportError, match=message):
        run_historical_physical_import(
            db_session,
            ledger_generation_id=generation.id,
            client=client,
            from_exclusive=start,
            to_inclusive=cutoff,
        )

    db_session.refresh(generation)
    assert generation.physical_import_batch_id == baseline.id
    assert db_session.query(models.LedgerBuildBatch).count() == 0
    assert db_session.query(models.StockLedgerEntry).count() == 0
    assert client.write_calls == []
