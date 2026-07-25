from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re

import pytest

from app import models
from app.services.item_ledger.ingest import (
    DEFAULT_MAX_ATTEMPTS,
    EMPTY_GUID,
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


class _RecorderClient:
    def __init__(self, lines_by_ref):
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
        client=object(),
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
        client=object(),
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
        client=object(),
    )

    assert first.from_checkpoint is False
    assert second.from_checkpoint is True
    assert second.checkpoint_id == first.checkpoint_id
    assert second.changed_recorders == 0


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
            client=object(),
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
            client=object(),
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
            client=object(),
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
        client=object(),
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
        client=object(),
    )
    assert result.terminal_physical_import_batch_id > int(parent_batch.id)


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
