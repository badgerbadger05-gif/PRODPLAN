from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import re

import pytest

from app import models
from app.services.item_ledger.ingest import (
    DEFAULT_MAX_ATTEMPTS,
    EMPTY_GUID,
    HistoricalPullBeyondCutoffError,
    PullResult,
)
from app.services.item_ledger.physical_visibility import visible_sles_for_generation
from app.services.item_ledger.physical_refresh_import import (
    CHECKPOINT_KEY_PREFIX,
    CHECKPOINT_VERSION,
    PhysicalRefreshImportError,
    PhysicalRefreshImportResult,
    run_physical_recorder_audit,
)


def _accepted_parent(db_session, key: str = "accepted-parent"):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key=f"accepted-physical-{key}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={"origin": "test"},
        completed_at=cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key=f"physical-parent-{key}",
        status="accepted",
        cutoff=cutoff,
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        capabilities={"physical_ledger": True},
        physical_import_batch=batch,
        algorithm_version="accepted/1",
        accepted_at=cutoff,
    )
    db_session.add_all([batch, parent])
    db_session.flush()
    return parent, batch


def _building_target(db_session, parent: models.LedgerGeneration, key: str = "target"):
    target = models.LedgerGeneration(
        generation_key=f"physical-target-{key}",
        status="building",
        cutoff=(parent.cutoff + timedelta(days=1)),
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(parent.id),
        },
        physical_import_batch=parent.physical_import_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    db_session.add(target)
    db_session.flush()
    return target


def _seed_visible_entry(
    db_session,
    physical_import_batch_id: int,
    *,
    recorder_type: str,
    recorder_ref: str,
    posting_at: datetime,
):
    item = models.Item(item_code=f"{recorder_type}-{recorder_ref}", item_name="Test")
    db_session.add(item)
    db_session.flush()
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=int(physical_import_batch_id),
        source_content_hash="x" * 64,
        item_id=item.item_id,
        qty=Decimal("1"),
        posting_at=posting_at,
        recorder_type=recorder_type,
        recorder_ref=recorder_ref,
        line_no="1",
    ))
    db_session.flush()


class _RegisterClient:
    """Client stub for the discovery scan the audit now performs.

    ``register_rows`` are the flat ``_RecordType`` rows 1C would return for the
    discovery range; each carries Period + Recorder identity only, which is all
    discovery selects.
    """

    def __init__(self, register_rows=()):
        self.register_rows = list(register_rows)
        self.discovery_filters: list[str] = []

    def _make_request(self, entity_name, params):
        assert entity_name == "AccumulationRegister_ЗапасыНаСкладах_RecordType"
        filter_query = str(params.get("$filter") or "")
        self.discovery_filters.append(filter_query)
        if int(params.get("$skip") or 0):
            return {"value": []}
        bounds = re.findall(r"datetime'([^']+)'", filter_query)
        low, high = (datetime.fromisoformat(bound) for bound in bounds)
        return {
            "value": [
                row for row in self.register_rows
                if low < datetime.fromisoformat(row["Period"]) <= high
            ]
        }

    def discovery_window_starts(self):
        return [
            datetime.fromisoformat(re.findall(r"datetime'([^']+)'", flt)[0])
            for flt in self.discovery_filters
        ]


def _as_register_time(value: datetime) -> datetime:
    """1C receives OData filter bounds as naive Europe/Moscow local time."""
    return value.astimezone(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)


def _register_row(recorder_type, recorder_ref, period="2026-07-10T10:00:00"):
    return {
        "Period": period,
        "Recorder": recorder_ref,
        "Recorder_Type": f"StandardODATA.{recorder_type}",
        "LineNumber": "1",
        "Active": True,
        "RecordType": "",
        "Номенклатура_Key": "",
        "Характеристика_Key": "",
        "Организация_Key": "",
        "СтруктурнаяЕдиница_Key": "",
        "Количество": 1,
    }


class _RecorderClient(_RegisterClient):
    def __init__(self, lines_by_ref, register_rows=()):
        super().__init__(register_rows)
        self.lines_by_ref = lines_by_ref

    def get_all(self, entity_name, filter_query=None, **kwargs):
        if entity_name != "AccumulationRegister_ЗапасыНаСкладах":
            return []
        match = re.search(r"guid'([^']+)'", filter_query or "")
        recorder_ref = match.group(1) if match else ""
        return [{"RecordSet": list(self.lines_by_ref.get(recorder_ref, []))}]


def _movement_line(qty):
    return {
        "Period": "2026-07-10T10:00:00",
        "LineNumber": "1",
        "Active": True,
        "RecordType": "Receipt",
        "Организация_Key": "ORG-AUDIT",
        "Номенклатура_Key": "ITEM-AUDIT-REF",
        "Характеристика_Key": EMPTY_GUID,
        "СтруктурнаяЕдиница_Key": "WH-AUDIT",
        "Количество": qty,
    }


def _real_audit_world(db_session, key):
    parent, batch = _accepted_parent(db_session, key)
    item = models.Item(
        item_code=f"AUDIT-{key}",
        item_name="Audit item",
        item_ref1c="ITEM-AUDIT-REF",
    )
    warehouse = models.StockWarehouse(
        warehouse_ref1c="WH-AUDIT",
        warehouse_name="Audit warehouse",
    )
    db_session.add_all([item, warehouse])
    db_session.flush()
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash="a" * 64,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="ORG-AUDIT",
        warehouse_ref1c="WH-AUDIT",
        qty=Decimal("5"),
        posting_at=datetime(2026, 7, 10, 10, tzinfo=timezone.utc),
        record_type="Receipt",
        recorder_type="Document_Receipt",
        recorder_ref=f"DOC-{key}",
        line_no="1",
        ingest_source="document_pull",
        active=True,
    ))
    target = _building_target(db_session, parent, key)
    db_session.commit()
    return parent, target


def test_physical_refresh_recorder_audit_runs_union_and_tracks_changed_recorders(db_session, monkeypatch):
    parent, parent_batch = _accepted_parent(db_session, "union")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_Receipt",
        recorder_ref="receipt-visible",
        posting_at=parent.cutoff - timedelta(hours=1),
    )
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_Receipt",
        recorder_ref="receipt-visible-2",
        posting_at=parent.cutoff - timedelta(hours=1),
    )

    db_session.add_all([
        models.StockRecorderPull(recorder_type="Document_Expense", recorder_ref="expense-pending", status="pending"),
        models.StockRecorderPull(
            recorder_type="Document_Receipt",
            recorder_ref="receipt-visible",
            status="done",
            line_count=4,
        ),
        models.StockRecorderPull(
            recorder_type="Document_Receipt",
            recorder_ref="receipt-visible-2",
            status="empty",
            line_count=0,
        ),
    ])
    db_session.commit()

    calls: list[tuple[str, str, bool, datetime]] = []
    outcomes = {
        ("Document_Expense", "expense-pending"): PullResult(
            status="done",
            inserted=1,
            touched_keys=[],
        ),
        ("Document_Receipt", "receipt-visible"): PullResult(
            status="done",
            inserted=0,
            touched_keys=[],
        ),
        ("Document_Receipt", "receipt-visible-2"): PullResult(
            status="empty",
            inserted=0,
            touched_keys=[],
        ),
    }

    def _pull_recorder_movements(
        db,
        recorder_type,
        recorder_ref,
        client,
        source,
        ledger_generation_id,
        max_posting_at,
        strict_historical=True,
        **kwargs,
    ):
        calls.append((recorder_type, recorder_ref, strict_historical, max_posting_at, kwargs.get("sequence_lock_held")))
        result = outcomes[(recorder_type, recorder_ref)]
        if result.inserted:
            batch = models.PhysicalImportBatch(
                batch_key=f"fake-audit:{recorder_type}:{recorder_ref}",
                status="completed",
                cutoff=target.cutoff,
                source_watermarks={
                    "recorder_type": recorder_type,
                    "recorder_ref": recorder_ref,
                },
            )
            db.add(batch)
            db.flush()
            result.physical_import_batch_id = int(batch.id)
        else:
            result.physical_import_batch_id = int(
                db.query(models.PhysicalImportBatch.id)
                .order_by(models.PhysicalImportBatch.id.desc())
                .first()[0]
            )
        return result

    target = _building_target(db_session, parent, "union")
    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull_recorder_movements,
    )

    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient(),
    )

    assert isinstance(result, PhysicalRefreshImportResult)
    assert result.from_checkpoint is False
    assert result.recorder_count == 3
    assert result.changed_recorders == 1
    assert result.terminal_physical_import_batch_id > int(parent.physical_import_batch_id)
    assert calls == [
        ("Document_Expense", "expense-pending", True, target.cutoff, None),
        ("Document_Receipt", "receipt-visible", True, target.cutoff, None),
        ("Document_Receipt", "receipt-visible-2", True, target.cutoff, None),
    ]

    checkpoint = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=int(target.id),
        stage="physical_import",
        batch_key=f"{CHECKPOINT_KEY_PREFIX}:{int(target.id)}:{CHECKPOINT_VERSION}",
    ).one()
    assert checkpoint.status == "completed"
    assert checkpoint.metrics["recorder_count"] == 3
    assert checkpoint.metrics["recorder_audit_checksum"] == result.recorder_audit_checksum
    assert result.recorder_audit_checksum == target.source_watermarks["recorder_audit"]["checksum"]


def test_physical_refresh_recorder_audit_reuses_completed_checkpoint_without_client_calls(db_session, monkeypatch):
    parent, parent_batch = _accepted_parent(db_session, "checkpoint")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_Receipt",
        recorder_ref="receipt-visible",
        posting_at=parent.cutoff - timedelta(hours=1),
    )
    db_session.add(models.StockRecorderPull(
        recorder_type="Document_Receipt",
        recorder_ref="receipt-visible",
        status="done",
        line_count=1,
    ))
    db_session.flush()
    target = _building_target(db_session, parent, "checkpoint")

    created_calls: list[tuple[str, str]] = []

    def _initial_pull(db, *args, **kwargs):
        created_calls.append(("called", ""))
        batch = models.PhysicalImportBatch(
            batch_key="fake-checkpoint-pull",
            status="completed",
            cutoff=target.cutoff,
            source_watermarks={
                "recorder_type": args[0],
                "recorder_ref": args[1],
            },
        )
        db.add(batch)
        db.flush()
        return PullResult(
            status="done",
            inserted=1,
            physical_import_batch_id=int(batch.id),
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _initial_pull,
    )
    first = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient(),
    )
    assert created_calls == [("called", "")]

    later_window = models.PhysicalImportBatch(
        batch_key="later-historical-window",
        status="completed",
        cutoff=target.cutoff,
        source_watermarks={
            "previous_import_batch_id": first.terminal_physical_import_batch_id
        },
        completed_at=target.cutoff,
    )
    db_session.add(later_window)
    db_session.flush()
    target.physical_import_batch_id = later_window.id
    db_session.commit()

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        lambda *args, **kwargs: pytest.fail("pull should not run after checkpoint"),
    )
    second = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient(),
    )

    assert first.from_checkpoint is False
    assert second.from_checkpoint is True
    assert second.checkpoint_id == first.checkpoint_id
    assert second.changed_recorders == 0


def test_incremental_audit_pulls_only_due_recorders(db_session, monkeypatch):
    parent, parent_batch = _accepted_parent(db_session, "incremental-only")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_Receipt",
        recorder_ref="old-visible-doc",
        posting_at=parent.cutoff - timedelta(days=5),
    )
    db_session.add(models.StockRecorderPull(
        recorder_type="Document_СборкаЗапасов",
        recorder_ref="changed-doc",
        status="pending",
    ))
    target = _building_target(db_session, parent, "incremental-only")
    db_session.commit()

    pulled: list[str] = []

    def _pull(db, recorder_type, recorder_ref, **kwargs):
        pulled.append(recorder_ref)
        return PullResult(
            status="done",
            inserted=0,
            physical_import_batch_id=int(parent_batch.id),
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient(),
        discovery_lookback=timedelta(0),
        audit_all_known_recorders=False,
    )

    assert pulled == ["changed-doc"]
    assert result.recorder_count == 1
    assert result.discovered_recorders == 0


def test_incremental_audit_pulls_due_and_truly_new_backdated_recorders(
    db_session, monkeypatch
):
    parent, parent_batch = _accepted_parent(db_session, "incremental-backdated")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_Receipt",
        recorder_ref="old-visible-doc",
        posting_at=parent.cutoff - timedelta(days=5),
    )
    db_session.add(models.StockRecorderPull(
        recorder_type="Document_СборкаЗапасов",
        recorder_ref="changed-doc",
        status="pending",
    ))
    target = _building_target(db_session, parent, "incremental-backdated")
    db_session.commit()

    client = _RegisterClient([
        _register_row(
            "Document_Receipt",
            "old-visible-doc",
            period="2026-07-20T10:00:00",
        ),
        _register_row(
            "Document_ПеремещениеЗапасов",
            "new-backdated-doc",
            period="2026-07-21T10:00:00",
        ),
    ])
    pulled: list[str] = []

    def _pull(db, recorder_type, recorder_ref, **kwargs):
        pulled.append(recorder_ref)
        return PullResult(
            status="done",
            inserted=0,
            physical_import_batch_id=int(parent_batch.id),
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=client,
        discovery_lookback=None,
        audit_all_known_recorders=False,
    )

    assert pulled == ["new-backdated-doc", "changed-doc"]
    assert result.recorder_count == 2
    assert result.discovered_recorders == 2
    assert result.backdated_recorders == 1


def test_incremental_audit_repulls_recorder_left_ahead_by_rejected_candidate(
    db_session, monkeypatch
):
    parent, parent_batch = _accepted_parent(db_session, "pull-state-drift")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_СборкаЗапасов",
        recorder_ref="older-revised-doc",
        posting_at=parent.cutoff - timedelta(days=30),
    )
    db_session.add(models.StockRecorderPull(
        recorder_type="Document_СборкаЗапасов",
        recorder_ref="older-revised-doc",
        status="done",
        line_count=6,
        source="physical_refresh_recorder_audit",
    ))
    target = _building_target(db_session, parent, "pull-state-drift")
    db_session.commit()

    pulled: list[str] = []

    def _pull(db, recorder_type, recorder_ref, **kwargs):
        pulled.append(recorder_ref)
        return PullResult(
            status="done",
            inserted=0,
            physical_import_batch_id=int(parent_batch.id),
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient(),
        discovery_lookback=timedelta(days=7),
        audit_all_known_recorders=False,
    )

    assert pulled == ["older-revised-doc"]
    assert result.recorder_count == 1
    checkpoint = db_session.get(models.LedgerBuildBatch, result.checkpoint_id)
    assert checkpoint.metrics["discovery"]["pull_state_drift_recorders"] == 1


def test_incremental_audit_defers_pull_state_drift_newer_than_cutoff(
    db_session, monkeypatch
):
    parent, parent_batch = _accepted_parent(db_session, "future-pull-state")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="future-revised-doc",
        posting_at=parent.cutoff - timedelta(days=30),
    )
    db_session.add(models.StockRecorderPull(
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="future-revised-doc",
        status="done",
        line_count=2,
        source="physical_refresh_recorder_audit",
    ))
    target = _building_target(db_session, parent, "future-pull-state")
    db_session.commit()

    def _pull(*args, **kwargs):
        raise HistoricalPullBeyondCutoffError("movement exceeds cutoff")

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient(),
        discovery_lookback=timedelta(days=7),
        audit_all_known_recorders=False,
    )

    assert result.recorder_count == 1
    checkpoint = db_session.get(models.LedgerBuildBatch, result.checkpoint_id)
    assert checkpoint.metrics["deferred_pull_state_drift"] == [{
        "recorder_type": "Document_ПеремещениеЗапасов",
        "recorder_ref": "future-revised-doc",
    }]


def test_incremental_audit_repulls_known_recorder_when_line_count_changed(
    db_session, monkeypatch
):
    parent, parent_batch = _accepted_parent(db_session, "incremental-revised")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="revised-doc",
        posting_at=parent.cutoff - timedelta(days=5),
    )
    target = _building_target(db_session, parent, "incremental-revised")
    db_session.commit()

    client = _RegisterClient([
        _register_row(
            "Document_ПеремещениеЗапасов",
            "revised-doc",
            period="2026-07-20T10:00:00",
        ),
        _register_row(
            "Document_ПеремещениеЗапасов",
            "revised-doc",
            period="2026-07-20T10:00:01",
        ),
    ])
    pulled: list[str] = []

    def _pull(db, recorder_type, recorder_ref, **kwargs):
        pulled.append(recorder_ref)
        return PullResult(
            status="done",
            inserted=0,
            physical_import_batch_id=int(parent_batch.id),
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=client,
        discovery_lookback=None,
        audit_all_known_recorders=False,
    )

    assert pulled == ["revised-doc"]
    assert result.recorder_count == 1
    checkpoint = db_session.get(models.LedgerBuildBatch, result.checkpoint_id)
    assert checkpoint.metrics["discovery"]["revised_recorders"] == 1


def test_incremental_audit_repulls_same_count_recorder_when_balance_key_changed(
    db_session, monkeypatch
):
    parent, parent_batch = _accepted_parent(db_session, "incremental-content")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="changed-key-doc",
        posting_at=parent.cutoff - timedelta(days=5),
    )
    target = _building_target(db_session, parent, "incremental-content")
    db_session.commit()

    changed_row = _register_row(
        "Document_ПеремещениеЗапасов",
        "changed-key-doc",
        period="2026-07-20T10:00:00",
    )
    changed_row["СтруктурнаяЕдиница_Key"] = "changed-warehouse"
    pulled: list[str] = []

    def _pull(db, recorder_type, recorder_ref, **kwargs):
        pulled.append(recorder_ref)
        return PullResult(
            status="done",
            inserted=0,
            physical_import_batch_id=int(parent_batch.id),
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient([changed_row]),
        discovery_lookback=None,
        audit_all_known_recorders=False,
    )

    assert pulled == ["changed-key-doc"]
    assert result.recorder_count == 1
    assert result.revised_recorders == 1


def test_incremental_audit_repulls_known_recorder_that_vanished(
    db_session, monkeypatch
):
    parent, parent_batch = _accepted_parent(db_session, "incremental-vanished")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="vanished-doc",
        posting_at=parent.cutoff - timedelta(days=5),
    )
    target = _building_target(db_session, parent, "incremental-vanished")
    db_session.commit()

    pulled: list[str] = []

    def _pull(db, recorder_type, recorder_ref, **kwargs):
        pulled.append(recorder_ref)
        return PullResult(
            status="empty",
            inserted=0,
            physical_import_batch_id=int(parent_batch.id),
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient([]),
        discovery_lookback=None,
        audit_all_known_recorders=False,
    )

    assert pulled == ["vanished-doc"]
    assert result.recorder_count == 1
    assert result.vanished_recorders == 1
    checkpoint = db_session.get(models.LedgerBuildBatch, result.checkpoint_id)
    assert checkpoint.metrics["discovery"]["vanished_recorders"] == 1


def test_physical_refresh_recorder_audit_rejects_pull_error_status(db_session, monkeypatch):
    parent, parent_batch = _accepted_parent(db_session, "error-status")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_Receipt",
        recorder_ref="receipt-visible",
        posting_at=parent.cutoff - timedelta(hours=1),
    )
    target = _building_target(db_session, parent, "error-status")

    def _pull_error(*args, **kwargs):
        return PullResult(status="error", error="boom")

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull_error,
    )
    with pytest.raises(
        PhysicalRefreshImportError,
        match="failed with status error",
    ):
        run_physical_recorder_audit(
            db_session,
            ledger_generation_id=target.id,
            parent_generation_id=parent.id,
            client=_RegisterClient(),
        )
    assert (
        db_session.query(models.LedgerBuildBatch)
        .filter_by(ledger_generation_id=target.id, stage="physical_import")
        .count()
    ) == 0


def test_physical_refresh_recorder_audit_rejects_nonzero_skips(db_session, monkeypatch):
    parent, parent_batch = _accepted_parent(db_session, "skip")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_Receipt",
        recorder_ref="receipt-visible",
        posting_at=parent.cutoff - timedelta(hours=1),
    )
    target = _building_target(db_session, parent, "skip")

    def _pull_skips(*args, **kwargs):
        return PullResult(status="done", inserted=1, skipped_unknown_item=1)

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull_skips,
    )
    with pytest.raises(
        PhysicalRefreshImportError,
        match="produced skipped movements",
    ):
        run_physical_recorder_audit(
            db_session,
            ledger_generation_id=target.id,
            parent_generation_id=parent.id,
            client=_RegisterClient(),
        )
    assert (
        db_session.query(models.LedgerBuildBatch)
        .filter_by(ledger_generation_id=target.id, stage="physical_import")
        .count()
    ) == 0


def test_physical_refresh_recorder_audit_rejects_global_terminal_interleaving(db_session):
    parent, _ = _accepted_parent(db_session, "interleave")
    interfering = models.PhysicalImportBatch(
        batch_key="interfering-terminal",
        status="completed",
        cutoff=parent.cutoff,
        source_watermarks={},
        completed_at=parent.cutoff,
    )
    db_session.add(interfering)
    db_session.commit()

    target = _building_target(db_session, parent, "interleave")
    with pytest.raises(
        PhysicalRefreshImportError,
        match="physical import sequence interleaved",
    ):
        run_physical_recorder_audit(
            db_session,
            ledger_generation_id=target.id,
            parent_generation_id=parent.id,
            client=_RegisterClient(),
        )


def test_physical_refresh_recorder_audit_collects_pending_and_retryable_error_only(db_session, monkeypatch):
    parent, parent_batch = _accepted_parent(db_session, "queue")
    target = _building_target(db_session, parent, "queue")
    db_session.add_all([
        models.StockRecorderPull(
            recorder_type="Document_ErrorRetry",
            recorder_ref="a",
            status="error",
            attempts=DEFAULT_MAX_ATTEMPTS - 1,
        ),
        models.StockRecorderPull(
            recorder_type="Document_ErrorRetry",
            recorder_ref="b",
            status="error",
            attempts=DEFAULT_MAX_ATTEMPTS,
        ),
        models.StockRecorderPull(
            recorder_type="Document_Pending",
            recorder_ref="a",
            status="pending",
        ),
    ])
    db_session.commit()

    calls: list[tuple[str, str]] = []
    def _queue_pull(db, recorder_type, recorder_ref, **kwargs):
        calls.append((recorder_type, recorder_ref))
        return PullResult(
            status="done",
            inserted=0,
            physical_import_batch_id=int(parent_batch.id),
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _queue_pull,
    )
    run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient(),
    )
    assert calls == [
        ("Document_ErrorRetry", "a"),
        ("Document_Pending", "a"),
    ]


def test_recorder_audit_allows_noop_with_older_recorder_watermark(db_session, monkeypatch):
    parent, parent_batch = _accepted_parent(db_session, "older-watermark")
    for ref in ("rec-a", "rec-b"):
        _seed_visible_entry(
            db_session,
            int(parent_batch.id),
            recorder_type="Document_Receipt",
            recorder_ref=ref,
            posting_at=parent.cutoff - timedelta(hours=1),
        )
    target = _building_target(db_session, parent, "older-watermark")

    def _pull(db, recorder_type, recorder_ref, **kwargs):
        if recorder_ref == "rec-a":
            batch = models.PhysicalImportBatch(
                batch_key="audit-new-a",
                status="completed",
                cutoff=target.cutoff,
                source_watermarks={
                    "recorder_type": recorder_type,
                    "recorder_ref": recorder_ref,
                },
            )
            db.add(batch)
            db.flush()
            return PullResult(status="done", inserted=1, physical_import_batch_id=batch.id)
        return PullResult(status="empty", physical_import_batch_id=parent_batch.id)

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient(),
    )
    assert result.terminal_physical_import_batch_id > int(parent_batch.id)


def test_every_recorder_type_prodplan_writes_itself_is_treated_as_synthetic():
    """Filtering the 1C register by a synthetic recorder makes 1C reject the request.

    The audit collects recorder identities from the ledger, so any row PRODPLAN
    writes under its own recorder type would otherwise be re-pulled as if it
    were a document. That is a whole refresh lost, discovered only on the next
    run — which is exactly what the opening adjustment caused.
    """
    from app.services.item_ledger.opening_balance_reconcile import (
        ADJUSTMENT_RECORDER_TYPE,
    )
    from app.services.item_ledger.physical import SEED_RECORDER_TYPE
    from app.services.item_ledger.physical_refresh_import import (
        _SYNTHETIC_RECORDER_TYPES,
    )

    for recorder_type in (SEED_RECORDER_TYPE, ADJUSTMENT_RECORDER_TYPE):
        assert recorder_type in _SYNTHETIC_RECORDER_TYPES, (
            f"{recorder_type!r} is written by PRODPLAN but the audit would "
            "try to re-pull it from 1C"
        )


def test_opening_adjustment_rows_never_enter_the_audit(db_session, monkeypatch):
    """End-to-end: a generation carrying opening adjustments audits cleanly."""
    parent, parent_batch = _accepted_parent(db_session, "synthetic")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="opening_adjustment",
        recorder_ref="opening_adjustment:2026-06-02:deadbeef",
        posting_at=parent.cutoff - timedelta(days=20),
    )
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="seed",
        recorder_ref="seed:2026-06-02:cafe",
        posting_at=parent.cutoff - timedelta(days=20),
    )
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="real-doc",
        posting_at=parent.cutoff - timedelta(days=1),
    )
    target = _building_target(db_session, parent, "synthetic")
    db_session.commit()

    pulled: list[str] = []

    def _pull(db, recorder_type, recorder_ref, **kwargs):
        pulled.append(recorder_ref)
        return PullResult(
            status="done", inserted=0, physical_import_batch_id=int(parent_batch.id)
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RegisterClient(),
    )

    assert pulled == ["real-doc"]


def test_backdated_recorder_absent_from_ledger_joins_the_audit(db_session, monkeypatch):
    """A document dated behind the parent cutoff but posted after it.

    Its register Period sits in a forward window that already closed, so the
    forward scan will never revisit it and the ledger has never heard of it.
    Discovery is the only thing that can bring it in.
    """
    parent, parent_batch = _accepted_parent(db_session, "backdated")
    _seed_visible_entry(
        db_session,
        int(parent_batch.id),
        recorder_type="Document_СборкаЗапасов",
        recorder_ref="known-doc",
        posting_at=parent.cutoff - timedelta(days=3),
    )
    target = _building_target(db_session, parent, "backdated")
    db_session.commit()

    calls: list[tuple[str, str]] = []

    def _pull(db, recorder_type, recorder_ref, **kwargs):
        calls.append((recorder_type, recorder_ref))
        return PullResult(
            status="done",
            inserted=0,
            physical_import_batch_id=int(parent_batch.id),
        )

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        _pull,
    )
    client = _RegisterClient([
        _register_row("Document_СборкаЗапасов", "known-doc", "2026-07-20T09:00:00"),
        _register_row("Document_СборкаЗапасов", "backdated-doc", "2026-07-22T09:29:01"),
    ])
    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=client,
    )

    assert ("Document_СборкаЗапасов", "backdated-doc") in calls
    assert result.recorder_count == 2
    assert result.discovered_recorders == 2
    assert result.backdated_recorders == 1
    checkpoint = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=int(target.id),
        batch_key=f"{CHECKPOINT_KEY_PREFIX}:{int(target.id)}:{CHECKPOINT_VERSION}",
    ).one()
    assert checkpoint.metrics["discovery"]["backdated"] == [
        {
            "recorder_type": "Document_СборкаЗапасов",
            "recorder_ref": "backdated-doc",
        }
    ]


def test_backdated_recorder_movements_reach_the_refreshed_generation(db_session):
    """End-to-end: the missing expense lands in the child, not the parent."""
    parent, target = _real_audit_world(db_session, "BACKDATED")
    client = _RecorderClient(
        {
            "DOC-BACKDATED": [_movement_line(5)],
            "LATE-DOC": [dict(_movement_line(7), RecordType="Expense")],
        },
        register_rows=[
            _register_row("Document_Receipt", "DOC-BACKDATED"),
            _register_row("Document_СборкаЗапасов", "LATE-DOC", "2026-07-22T09:29:01"),
        ],
    )

    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=client,
    )

    assert result.backdated_recorders == 1
    assert [row.qty for row in visible_sles_for_generation(db_session, parent.id)] == [
        Decimal("5")
    ]
    assert sorted(
        (row.recorder_ref, row.qty)
        for row in visible_sles_for_generation(db_session, target.id)
    ) == [("DOC-BACKDATED", Decimal("5")), ("LATE-DOC", Decimal("-7"))]


def test_recorder_audit_discovery_is_floored_at_the_opening_boundary(db_session, monkeypatch):
    opening_at = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    db_session.add(models.PhysicalImportBatch(
        batch_key="historical-bootstrap-opening:g1:hash",
        status="completed",
        cutoff=opening_at,
        source_watermarks={
            # ensure_physical_import_batch rewrites `source` to the seed ingest
            # source; only `opening_at` reliably marks the opening boundary.
            "source": "seed",
            "anchor_period": opening_at.date().isoformat(),
            "opening_at": opening_at.isoformat(),
        },
        completed_at=opening_at,
    ))
    db_session.flush()
    parent, _parent_batch = _accepted_parent(db_session, "opening-floor")
    assert parent.cutoff - opening_at == timedelta(days=10)
    target = _building_target(db_session, parent, "opening-floor")
    db_session.commit()

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        lambda *args, **kwargs: pytest.fail("no recorder to pull"),
    )
    client = _RegisterClient()
    run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=client,
    )

    starts = client.discovery_window_starts()
    assert min(starts) == _as_register_time(opening_at)
    # 10 days at the 7-day discovery window: one full window plus the remainder.
    assert len(starts) == 2


def test_recorder_audit_discovery_respects_explicit_lookback(db_session, monkeypatch):
    parent, _ = _accepted_parent(db_session, "lookback")
    target = _building_target(db_session, parent, "lookback")
    db_session.commit()

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        lambda *args, **kwargs: pytest.fail("no recorder to pull"),
    )
    client = _RegisterClient()
    run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=client,
        discovery_lookback=timedelta(days=7),
        discovery_window_size=timedelta(days=7),
    )

    starts = client.discovery_window_starts()
    assert len(starts) == 1
    parent_cutoff = parent.cutoff.replace(tzinfo=timezone.utc)
    assert starts[0] == _as_register_time(parent_cutoff - timedelta(days=7))


def test_recorder_audit_fails_closed_when_discovery_cannot_read_the_register(
    db_session, monkeypatch
):
    parent, _ = _accepted_parent(db_session, "discovery-down")
    target = _building_target(db_session, parent, "discovery-down")
    db_session.commit()

    class _BrokenClient:
        def _make_request(self, *args, **kwargs):
            raise RuntimeError("1C unavailable")

    monkeypatch.setattr(
        "app.services.item_ledger.physical_refresh_import.pull_recorder_movements",
        lambda *args, **kwargs: pytest.fail("audit must not pull after discovery failed"),
    )
    with pytest.raises(
        PhysicalRefreshImportError,
        match="backdated recorder discovery failed",
    ):
        run_physical_recorder_audit(
            db_session,
            ledger_generation_id=target.id,
            parent_generation_id=parent.id,
            client=_BrokenClient(),
        )
    assert (
        db_session.query(models.LedgerBuildBatch)
        .filter_by(ledger_generation_id=target.id, stage="physical_import")
        .count()
    ) == 0


def test_old_recorder_correction_is_revisioned_inside_refresh(db_session):
    parent, target = _real_audit_world(db_session, "CORRECT")

    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RecorderClient({"DOC-CORRECT": [_movement_line(8)]}),
    )

    assert result.changed_recorders == 1
    assert result.terminal_physical_import_batch_id > int(
        parent.physical_import_batch_id
    )
    assert [row.qty for row in visible_sles_for_generation(
        db_session, parent.id
    )] == [Decimal("5")]
    assert [row.qty for row in visible_sles_for_generation(
        db_session, target.id
    )] == [Decimal("8")]


def test_old_recorder_deletion_creates_tombstone_inside_refresh(db_session):
    parent, target = _real_audit_world(db_session, "DELETE")

    result = run_physical_recorder_audit(
        db_session,
        ledger_generation_id=target.id,
        parent_generation_id=parent.id,
        client=_RecorderClient({"DOC-DELETE": []}),
    )

    assert result.changed_recorders == 1
    assert visible_sles_for_generation(db_session, parent.id)
    assert visible_sles_for_generation(db_session, target.id) == []
    edge = db_session.query(models.StockLedgerFactSupersession).one()
    assert edge.new_sle_id is None
