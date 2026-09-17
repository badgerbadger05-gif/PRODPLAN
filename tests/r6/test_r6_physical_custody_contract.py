"""R6 red gate: current physical stock, senior holds, and custody boundaries.

These tests intentionally exercise the small canonical folds independently of
generation storage.  A generation is provenance for a result; it is not a
second physical quantity or a reason to release a reservation hold.
"""

from decimal import Decimal
from types import SimpleNamespace
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from app import models
from app.services.item_ledger.current_physical import (
    CurrentPhysicalStateError,
    fold_current_stock,
    senior_hold_qty,
)
from app.services.item_ledger.physical import LedgerKey, rebuild_running_balance
from app.services.mrp_stock_helpers import planning_stock_by_item
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C
from app.services.production_material_custody_projection import (
    _late_events_behind_baseline,
)


def test_stock_bin_migration_allows_a_new_building_only_physical_key():
    """R6 upgrade keeps explicit BUILDING staging absent from accepted fold."""
    sa = pytest.importorskip("sqlalchemy")
    path = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260910_08_r6_compact_stock_bin.py"
    )
    spec = spec_from_file_location("r6_stock_bin_migration", path)
    migration = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE planning_truth_state (id INTEGER PRIMARY KEY, current_generation_id INTEGER)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE ledger_generation (id INTEGER PRIMARY KEY, status VARCHAR(16))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE stock_bin (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, "
            "characteristic_ref VARCHAR(36), organization_ref VARCHAR(36), "
            "warehouse_ref1c VARCHAR(36), ledger_generation_id INTEGER NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO planning_truth_state VALUES (1, 10)"
        )
        conn.exec_driver_sql(
            "INSERT INTO ledger_generation VALUES (10, 'accepted'), (11, 'building')"
        )
        conn.exec_driver_sql(
            "INSERT INTO stock_bin VALUES "
            "(1, 1, '', 'ORG', 'WH', 10), (2, 2, '', 'ORG', 'WH', 11)"
        )
        migration._deduplicate(conn)


def test_stock_bin_migration_allows_key_absent_from_current_accepted_fold():
    """Historical-only keys are pruned, not fabricated into current stock."""
    sa = pytest.importorskip("sqlalchemy")
    path = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260910_08_r6_compact_stock_bin.py"
    )
    spec = spec_from_file_location("r6_stock_bin_migration_missing_current", path)
    migration = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE planning_truth_state (id INTEGER PRIMARY KEY, current_generation_id INTEGER)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE ledger_generation (id INTEGER PRIMARY KEY, status VARCHAR(16))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE stock_bin (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, "
            "characteristic_ref VARCHAR(36), organization_ref VARCHAR(36), "
            "warehouse_ref1c VARCHAR(36), ledger_generation_id INTEGER NOT NULL)"
        )
        conn.exec_driver_sql("INSERT INTO planning_truth_state VALUES (1, 10)")
        conn.exec_driver_sql(
            "INSERT INTO ledger_generation VALUES "
            "(9, 'accepted'), (10, 'accepted'), (12, 'failed')"
        )
        conn.exec_driver_sql(
            "INSERT INTO stock_bin VALUES "
            "(1, 673, '', 'ORG', 'WH', 9), (2, 673, '', 'ORG', 'WH', 12)"
        )
        # There is no row for this key in accepted generation 10.  The
        # migration must leave the key without a current owner rather than
        # selecting either historical row or inventing a zero row.
        migration._deduplicate(conn)


def test_compact_stock_fold_keeps_full_key_and_negative_physical_quantity():
    rows = [
        {"item_id": 10, "characteristic_ref": "", "organization_ref": "ORG-A", "warehouse_ref1c": "WH-1", "qty": "-3.125"},
        {"item_id": 10, "characteristic_ref": "", "organization_ref": "ORG-A", "warehouse_ref1c": "WH-1", "qty": "1.000"},
        {"item_id": 10, "characteristic_ref": "", "organization_ref": "ORG-B", "warehouse_ref1c": "WH-1", "qty": "5.000"},
        {"item_id": 10, "characteristic_ref": "", "organization_ref": "ORG-A", "warehouse_ref1c": "WH-2", "qty": "2.000"},
    ]

    result = fold_current_stock(rows)

    assert result[(10, "", "ORG-A", "WH-1")].on_hand == Decimal("-2.125")
    assert result[(10, "", "ORG-B", "WH-1")].on_hand == Decimal("5")
    assert result[(10, "", "ORG-A", "WH-2")].on_hand == Decimal("2")


def test_senior_hold_receipt_does_not_release_but_assigned_expense_does():
    allocations = [
        SimpleNamespace(allocation_role="replenishment_receipt", allocated_qty=Decimal("4")),
        SimpleNamespace(allocation_role="material_consumption", allocated_qty=Decimal("2")),
    ]

    assert senior_hold_qty(Decimal("5"), allocations) == Decimal("3")
    assert senior_hold_qty(Decimal("5"), allocations[:1]) == Decimal("5")
    assert senior_hold_qty(Decimal("2"), allocations) == Decimal("0")


def test_senior_hold_rejects_unknown_allocation_role():
    with pytest.raises(CurrentPhysicalStateError, match="allocation role"):
        senior_hold_qty(
            Decimal("1"),
            [SimpleNamespace(allocation_role="unknown", allocated_qty=Decimal("1"))],
        )


def test_conservation_is_role_separate_and_decimal_exact():
    allocations = [
        SimpleNamespace(allocation_role="material_consumption", allocated_qty=Decimal("1.125")),
        SimpleNamespace(allocation_role="replenishment_receipt", allocated_qty=Decimal("2.250")),
    ]
    assert senior_hold_qty(Decimal("4.000"), allocations) == Decimal("2.875")


def test_internal_transfer_and_return_keep_signs_and_foreign_organization_isolated():
    rows = [
        {"item_id": 12, "organization_ref": "ORG-A", "warehouse_ref1c": "WH-A", "qty": "-4"},
        {"item_id": 12, "organization_ref": "ORG-A", "warehouse_ref1c": "WH-B", "qty": "4"},
        {"item_id": 12, "organization_ref": "ORG-A", "warehouse_ref1c": "WH-B", "qty": "-1"},
        {"item_id": 12, "organization_ref": "ORG-B", "warehouse_ref1c": "WH-B", "qty": "9"},
    ]
    folded = fold_current_stock(rows)
    assert folded[(12, "", "ORG-A", "WH-A")].on_hand == Decimal("-4")
    assert folded[(12, "", "ORG-A", "WH-B")].on_hand == Decimal("3")
    assert folded[(12, "", "ORG-B", "WH-B")].on_hand == Decimal("9")


def test_current_reader_does_not_fall_back_to_requested_generation(db_session, building_ledger_generation):
    item = models.Item(item_code="R6-CURRENT", item_name="R6 current", unit="шт", status="active")
    db_session.add(item)
    db_session.flush()
    db_session.add(models.StockBin(
        ledger_generation_id=building_ledger_generation.id + 100,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH-R6",
        on_hand=Decimal("-2.500"),
    ))
    db_session.flush()
    with pytest.raises(ValueError, match="current StockBin provenance"):
        planning_stock_by_item(
            db_session, ledger_generation_id=building_ledger_generation.id
        )


def test_late_custody_event_is_explicitly_visible_for_baseline_rewind(db_session):
    event = models.ProductionMaterialCustodyEvent(
        product_id=1,
        component_item_id=2,
        source_kind="issue_created",
        effective_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        location_kind="workshop",
        warehouse_ref1c="WH-R6",
        delta_qty=Decimal("1"),
        idempotency_key="r6-late-custody",
    )
    db_session.add(event)
    db_session.commit()
    assert _late_events_behind_baseline(
        db_session,
        baseline_cutoff=datetime(2026, 7, 5, tzinfo=timezone.utc),
        baseline_high_watermark_id=0,
        target_high_watermark_id=event.id,
    ) == [event.id]


def test_building_candidate_stock_is_not_visible_until_explicit_publication(
    db_session, building_ledger_generation
):
    item = models.Item(item_code="R6-MVCC", item_name="R6 MVCC", unit="шт", status="active")
    db_session.add(item)
    db_session.flush()
    key = LedgerKey(item.item_id, "", DEFAULT_ORGANIZATION_REF1C, "WH-R6")
    db_session.add(models.StockBin(
        ledger_generation_id=building_ledger_generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH-R6",
        on_hand=Decimal("10"),
        is_current=True,
    ))
    batch = models.PhysicalImportBatch(batch_key="r6-mvcc-batch", status="completed")
    candidate = models.LedgerGeneration(
        generation_key="r6-mvcc-candidate", status="building", source_watermarks={},
        capabilities={}, physical_import_batch=batch, algorithm_version="r6", replay_version="r6",
    )
    db_session.add(candidate)
    db_session.flush()
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=batch.id, source_content_hash="r6-mvcc".ljust(64, "0"),
        item_id=item.item_id, characteristic_ref="", organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH-R6", qty=Decimal("-4"), qty_after=Decimal("0"),
        posting_at=datetime(2026, 9, 10), record_type="Expense", movement_kind="consume",
        recorder_type="Document_R6", recorder_ref="r6-mvcc", line_no="1", ingest_source="test",
    ))
    db_session.flush()
    rebuild_running_balance(db_session, key, ledger_generation_id=candidate.id, publish_current=False)
    assert planning_stock_by_item(db_session, building_ledger_generation.id)[item.item_id] == 10.0
    staged = db_session.query(models.StockBin).filter_by(ledger_generation_id=candidate.id).one()
    assert staged.is_current is False and staged.on_hand == Decimal("-4")
    rebuild_running_balance(db_session, key, ledger_generation_id=candidate.id, publish_current=True)
    with pytest.raises(ValueError, match="current StockBin provenance"):
        planning_stock_by_item(db_session, building_ledger_generation.id)
    db_session.get(models.PlanningTruthState, 1).current_generation_id = candidate.id
    assert planning_stock_by_item(db_session, candidate.id)[item.item_id] == -4.0


def test_reservation_consumption_default_is_not_current_without_explicit_publication():
    column = models.ReservationConsumptionAllocation.__table__.c.is_current
    assert column.default is not None and column.default.arg is False
