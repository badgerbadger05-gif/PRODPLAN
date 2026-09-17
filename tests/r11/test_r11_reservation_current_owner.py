"""Focused contract tests for stable reservation current ownership."""

from datetime import date
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import models
from app.services.item_ledger.reservation import (
    append_realization_event,
    fold_reservation_entry,
)
from app.services.item_ledger.reservation_current import (
    ReservationCurrentError,
    publish_current_reservations,
)
from app.services.item_ledger.reservation import (
    reservation_event_identity,
    reservation_event_origin_kind,
)


def _migration():
    path = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260914_01_reservation_current_owner.py"
    )
    spec = spec_from_file_location("reservation_current_owner", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _entry():
    return SimpleNamespace(requirement_id=17, realization_mode="buy")


def _db_reservation(db_session, *, generation_key: str, status: str = "building"):
    item = models.Item(item_code=f"R11-{generation_key}", item_name="R11")
    batch = models.PhysicalImportBatch(
        batch_key=f"r11-batch-{generation_key}",
        status="completed",
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"r11-generation-{generation_key}",
        status=status,
        physical_import_batch=batch,
        source_watermarks={},
        capabilities={},
        algorithm_version="r11-test",
    )
    run = models.PlanningRun(config_snapshot={})
    db_session.add_all([item, generation, run])
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
    )
    db_session.add(requirement)
    db_session.flush()
    entry = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        run_id=run.run_id,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 7, 1),
        priority_period_to=date(2026, 7, 31),
        realization_mode="buy",
        reserved_qty=Decimal("5.000"),
        replenishment_required_qty=Decimal("5.000"),
        current_identity=f"reservation:req:{requirement.id}:mode:buy",
        owner_kind="building" if status == "building" else "current",
        is_current=status == "accepted",
    )
    db_session.add(entry)
    db_session.flush()
    return generation, entry


def test_event_identity_is_stable_across_replay_cycles():
    entry = _entry()
    values = dict(
        event_kind="realize",
        reserved_delta=Decimal("0"),
        realized_delta=Decimal("2.000"),
        sle_id=501,
        fact_ref="receipt-1",
        fact_line_ref="2",
        match_rule="fifo",
    )
    first = reservation_event_identity(entry, **values)
    repeated = reservation_event_identity(entry, **values)
    assert first == repeated
    assert first == reservation_event_identity(
        entry, **{**values, "reserved_delta": Decimal("0.000"), "realized_delta": Decimal("2.000")}
    )
    assert first == "reservation:req:17:mode:buy|realize|501|receipt-1|2|0.000|2.000|fifo"
    assert reservation_event_origin_kind(
        event_kind="realize", realized_delta=Decimal("2"), sle_id=501
    ) == "factual"


def test_negative_physical_event_is_semantic_correction_not_replay():
    assert reservation_event_origin_kind(
        event_kind="unrealize", realized_delta=Decimal("-1"), sle_id=501
    ) == "correction"
    assert reservation_event_origin_kind(
        event_kind="open", realized_delta=Decimal("0"), sle_id=None
    ) == "obligation"


def test_current_append_uses_prior_generation_seed_and_is_visible_in_fold(db_session):
    old_generation, entry = _db_reservation(
        db_session, generation_key="old", status="building"
    )
    entry.owner_kind = "current"
    entry.is_current = True
    old_event = models.ReservationEvent(
        ledger_generation_id=old_generation.id,
        reservation_id=entry.id,
        item_id=entry.item_id,
        event_kind="open",
        reserved_delta=Decimal("5.000"),
        realized_delta=Decimal("0.000"),
        idempotency_key="old-open",
        event_identity=reservation_event_identity(
            entry,
            event_kind="open",
            reserved_delta=Decimal("5.000"),
            realized_delta=Decimal("0.000"),
            sle_id=None,
            fact_ref="",
            fact_line_ref="",
            match_rule="",
        ),
        origin_kind="obligation",
        is_current=True,
    )
    db_session.add(old_event)
    db_session.flush()

    assert append_realization_event(
        db_session,
        entry,
        realized_delta=Decimal("2.000"),
        sle_id=None,
        fact_ref="receipt-1",
        fact_line_ref="1",
        match_rule="fifo",
        cycle_id="different-cycle",
        idempotency_key="new-fact",
        event_kind="realize",
    )
    new_event = db_session.query(models.ReservationEvent).filter_by(
        idempotency_key="new-fact"
    ).one()
    assert new_event.reserved_delta == Decimal("0.000")
    assert new_event.is_current is True
    assert fold_reservation_entry(db_session, entry.id).reserved_qty == Decimal("5.000")


def test_sqlite_publication_replaces_obligation_basis_without_doubling_fold(db_session):
    old_generation, owner = _db_reservation(
        db_session, generation_key="publish-old", status="accepted"
    )
    old_generation.status = "accepted"
    new_generation, stage = _db_reservation(
        db_session, generation_key="publish-new", status="building"
    )
    stage.requirement_id = owner.requirement_id
    stage.current_identity = owner.current_identity
    stage.reserved_qty = Decimal("7.000")
    stage.replenishment_required_qty = Decimal("7.000")
    db_session.flush()
    db_session.add(models.ReservationEvent(
        ledger_generation_id=old_generation.id,
        reservation_id=owner.id,
        item_id=owner.item_id,
        event_kind="open",
        reserved_delta=Decimal("5.000"),
        realized_delta=Decimal("0.000"),
        idempotency_key="publish-old-open",
        event_identity=reservation_event_identity(
            owner,
            event_kind="open",
            reserved_delta=Decimal("5.000"),
            realized_delta=Decimal("0.000"),
            sle_id=None,
            fact_ref="",
            fact_line_ref="",
            match_rule="",
        ),
        origin_kind="obligation",
        is_current=True,
    ))
    db_session.add(models.ReservationEvent(
        ledger_generation_id=new_generation.id,
        reservation_id=stage.id,
        item_id=stage.item_id,
        event_kind="open",
        reserved_delta=Decimal("7.000"),
        realized_delta=Decimal("0.000"),
        idempotency_key="publish-new-open",
        event_identity=reservation_event_identity(
            stage,
            event_kind="open",
            reserved_delta=Decimal("7.000"),
            realized_delta=Decimal("0.000"),
            sle_id=None,
            fact_ref="",
            fact_line_ref="",
            match_rule="",
        ),
        origin_kind="obligation",
        is_current=False,
    ))
    db_session.flush()
    result = publish_current_reservations(db_session, generation_id=new_generation.id)
    db_session.refresh(owner)
    assert result["published"] == 1
    assert owner.reserved_qty == Decimal("7.000")
    assert db_session.query(models.ReservationEntry).filter_by(id=stage.id).one_or_none() is None
    assert fold_reservation_entry(db_session, owner.id).reserved_qty == Decimal("7.000")
    assert db_session.query(models.ReservationCurrentChange).count() == 1


def test_postgres_publication_fails_closed_on_non_building_target_owner():
    generation = SimpleNamespace(status="building")

    class _Result:
        def scalar(self):
            return 1

    fake_db = SimpleNamespace(
        bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        get=lambda _model, _id: generation,
        execute=lambda *_args, **_kwargs: _Result(),
    )
    with pytest.raises(ReservationCurrentError, match="non-building owner"):
        publish_current_reservations(fake_db, generation_id=12)


def test_migration_is_set_based_and_has_no_latest_or_python_materialization():
    source = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260914_01_reservation_current_owner.py"
    ).read_text(encoding="utf-8")
    assert "mappings().all" not in source
    assert ".all()" not in source
    assert "ORDER BY id DESC" not in source
    assert ".mappings().all" not in source


def test_migration_bootstraps_exact_pointer_and_leaves_history_for_gc():
    source = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260914_01_reservation_current_owner.py"
    ).read_text(encoding="utf-8")
    validate = source.index("def _validate_and_backfill")
    event_update = source.index("UPDATE reservation_event AS event", validate)
    archive_table = source.index('"reservation_event_archive"')
    assert "INSERT INTO reservation_event_archive" not in source[validate:event_update]
    assert "reservation_current_owner_map" not in source[validate:event_update]
    assert "reservation_event_archive" in source[archive_table:]
    assert "server_default=\"legacy\"" in source
    assert "server_default=\"building\"" in source
    assert 'sa.Column("source_event_id"' not in source


def test_migration_has_no_historical_delete_or_dependency_rebind():
    source = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260914_01_reservation_current_owner.py"
    ).read_text(encoding="utf-8")
    validate = source[source.index("def _validate_and_backfill"):source.index("def upgrade")]
    assert "DELETE FROM reservation_entry" not in validate
    assert "DELETE FROM reservation_event" not in validate
    assert "reservation_consumption_allocation" not in validate
    assert "current_replenishment_audit" not in validate
    assert "replenishment_work_item" not in validate
    assert "purchase_export_obligation_allocation" not in validate
    assert source.count("UPDATE reservation_event AS event") == 1
    assert source.count("UPDATE reservation_entry\n") == 1
    assert "WHERE ledger_generation_id = :generation_id" in validate
    assert "owner_kind = 'current'" in validate
    assert "is_current = true" in validate


def test_sql_identity_contract_matches_runtime_shape_and_constraints():
    migration = _migration()
    sql = migration._EVENT_IDENTITY_SQL
    assert "'|kind:'" not in sql
    assert "'|sle:'" not in sql
    expected = "reservation:req:17:mode:buy|realize|501|receipt-1|2|0.000|2.000|fifo"
    assert reservation_event_identity(
        _entry(),
        event_kind="realize",
        reserved_delta=Decimal("0"),
        realized_delta=Decimal("2"),
        sle_id=501,
        fact_ref="receipt-1",
        fact_line_ref="2",
        match_rule="fifo",
    ) == expected
    assert "to_char(coalesce(event.reserved_delta, 0), 'FM999999999999990.000')" in sql
    assert "to_char(coalesce(event.realized_delta, 0), 'FM999999999999990.000')" in sql
    migration_source = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260914_01_reservation_current_owner.py"
    ).read_text(encoding="utf-8")
    assert "ck_reservation_entry_owner_kind" in migration_source
    assert "ck_reservation_event_origin_kind" in migration_source
    assert "ck_reservation_current_change_operation" in migration_source
    assert "ck_reservation_current_change_origin_kind" in migration_source


def test_postgres_publish_dedupes_stage_before_claiming_current_identity_slot():
    source = (
        Path(__file__).parents[2]
        / "backend/app/services/item_ledger/reservation_current.py"
    ).read_text(encoding="utf-8")
    semantic_dedupe = source.index("DELETE FROM reservation_event AS stage_event")
    current_rebind = source.index(
        "SET reservation_id = map.owner_id,\n               is_current = true"
    )
    assert semantic_dedupe < current_rebind
    assert "LEFT JOIN reservation_event AS event" in source
    assert "HAVING count(event.id) <> 1" in source
    migration_source = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260914_01_reservation_current_owner.py"
    ).read_text(encoding="utf-8")
    assert "LEFT JOIN reservation_event AS event" in migration_source
    assert "reservation_current_owner_map" not in migration_source
    assert "INSERT INTO reservation_event_archive" not in migration_source


@pytest.mark.parametrize("obligation_count", [0, 2])
def test_sqlite_publish_rejects_zero_or_duplicate_obligation_basis(
    db_session, obligation_count
):
    generation, stage = _db_reservation(
        db_session,
        generation_key=f"basis-{obligation_count}",
        status="building",
    )
    for index in range(obligation_count):
        db_session.add(models.ReservationEvent(
            ledger_generation_id=generation.id,
            reservation_id=stage.id,
            item_id=stage.item_id,
            event_kind="open",
            reserved_delta=Decimal("5.000"),
            realized_delta=Decimal("0.000"),
            idempotency_key=f"basis-{index}",
            event_identity=f"basis-event-{index}",
            origin_kind="obligation",
            is_current=False,
        ))
    db_session.flush()
    with pytest.raises(ReservationCurrentError, match="exactly one obligation basis"):
        publish_current_reservations(db_session, generation_id=generation.id)


def test_event_archive_generation_provenance_is_not_an_inbound_fk():
    current_change = models.ReservationCurrentChange.__table__
    assert any(
        fk.target_fullname == "reservation_entry.id"
        for fk in current_change.foreign_keys
    )
    assert not any(
        fk.target_fullname == "ledger_generation.id"
        for fk in current_change.foreign_keys
    )
    archive = models.ReservationEventArchive.__table__
    assert not any(
        fk.target_fullname == "ledger_generation.id"
        for fk in archive.foreign_keys
    )
    migration_text = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260914_01_reservation_current_owner.py"
    ).read_text(encoding="utf-8")
    current_change_sql = migration_text.split('"reservation_current_change"', 1)[1].split(
        '"reservation_event_archive"', 1
    )[0]
    assert 'sa.ForeignKeyConstraint(["reservation_id"], ["reservation_entry.id"]' in current_change_sql
    assert 'sa.ForeignKeyConstraint(["source_generation_id"]' not in current_change_sql
    archive_sql = migration_text.split('"reservation_event_archive"', 1)[1].split(
        "bind = op.get_bind()", 1
    )[0]
    assert "ForeignKeyConstraint" not in archive_sql
