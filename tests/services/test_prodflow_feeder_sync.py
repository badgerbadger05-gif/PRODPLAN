from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.services.prodflow_feeder_config import (
    ProdFlowFeederUnavailable,
    feeder_warehouse_for_items,
    publish_prodflow_feeder_assignments,
)


def _item_and_warehouse(db, suffix="A"):
    item = models.Item(item_code=f"PF-{suffix}", item_name=f"Item {suffix}")
    warehouse = models.StockWarehouse(
        warehouse_ref1c=f"warehouse-ref-{suffix}",
        warehouse_code=f"W-{suffix}",
        warehouse_name=f"Warehouse {suffix}",
        is_selected=True,
    )
    db.add_all((item, warehouse))
    db.flush()
    return item, warehouse


def test_complete_prodflow_snapshot_publishes_exact_item_feeder(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    result = publish_prodflow_feeder_assignments(
        db_session,
        positions=[{
            "name": "position-a", "item": item.item_code,
            "warehouse": warehouse.warehouse_name, "is_active": 1,
            "route_class": "weld",
        }],
        warehouses=[{
            "name": warehouse.warehouse_name,
            "custom_ref1c": warehouse.warehouse_ref1c,
        }],
        observed_at=datetime.now(timezone.utc),
    )
    db_session.commit()

    assert result["rows"] == 1
    assert feeder_warehouse_for_items(db_session, [item.item_id]) == {
        item.item_id: warehouse.warehouse_ref1c
    }


def test_prodflow_snapshot_rejects_ambiguous_or_unknown_warehouse(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    db_session.add(models.StockWarehouse(
        warehouse_ref1c="different-ref", warehouse_code="W-B",
        warehouse_name="Warehouse B", is_selected=True,
    ))
    db_session.flush()
    with pytest.raises(ProdFlowFeederUnavailable, match="ambiguous"):
        publish_prodflow_feeder_assignments(
            db_session,
            positions=[
                {"name": "one", "item": item.item_code, "warehouse": "W1", "is_active": 1},
                {"name": "two", "item": item.item_code, "warehouse": "W2", "is_active": 1},
            ],
            warehouses=[
                {"name": "W1", "custom_ref1c": warehouse.warehouse_ref1c},
                {"name": "W2", "custom_ref1c": "different-ref"},
            ],
            observed_at=datetime.now(timezone.utc),
        )


def test_prodflow_manifest_missing_and_missing_item_fail_closed(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    with pytest.raises(ProdFlowFeederUnavailable, match="not imported"):
        feeder_warehouse_for_items(db_session, [item.item_id])

    observed = datetime.now(timezone.utc) - timedelta(days=365)
    publish_prodflow_feeder_assignments(
        db_session,
        positions=[{
            "name": "position-a", "item": item.item_code,
            "warehouse": warehouse.warehouse_name, "is_active": 1,
        }],
        warehouses=[{
            "name": warehouse.warehouse_name,
            "custom_ref1c": warehouse.warehouse_ref1c,
        }],
        observed_at=observed,
    )
    db_session.commit()
    assert feeder_warehouse_for_items(db_session, [item.item_id])
    other = models.Item(item_code="PF-MISSING", item_name="Missing")
    db_session.add(other)
    db_session.commit()
    with pytest.raises(ProdFlowFeederUnavailable, match="missing"):
        feeder_warehouse_for_items(db_session, [other.item_id])


def test_inactive_prodflow_position_removes_previous_assignment(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    retained = models.Item(item_code="PF-RETAINED", item_name="Retained")
    db_session.add(retained)
    db_session.flush()
    now = datetime.now(timezone.utc)
    publish_prodflow_feeder_assignments(
        db_session,
        positions=[
            {"name": "p", "item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1},
            {"name": "r", "item": retained.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1},
        ],
        warehouses=[{"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c}],
        observed_at=now,
    )
    publish_prodflow_feeder_assignments(
        db_session,
        positions=[
            {"name": "p", "item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": 0},
            {"name": "r", "item": retained.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1},
        ],
        warehouses=[{"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c}],
        observed_at=now + timedelta(minutes=1),
    )
    db_session.commit()
    with pytest.raises(ProdFlowFeederUnavailable, match="missing"):
        feeder_warehouse_for_items(db_session, [item.item_id])


def test_invalid_refresh_does_not_replace_last_complete_mirror(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    observed = datetime.now(timezone.utc)
    first = publish_prodflow_feeder_assignments(
        db_session,
        positions=[{"name": "good", "item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1}],
        warehouses=[{"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c}],
        observed_at=observed,
    )
    db_session.commit()

    with pytest.raises(ProdFlowFeederUnavailable, match="no custom_ref1c"):
        publish_prodflow_feeder_assignments(
            db_session,
            positions=[{"name": "bad", "item": item.item_code, "warehouse": "Unknown", "is_active": 1}],
            warehouses=[],
            observed_at=observed + timedelta(minutes=1),
        )

    manifest = db_session.get(models.ProdFlowFeederManifest, 1)
    assert manifest.source_revision == first["source_revision"]
    assert feeder_warehouse_for_items(db_session, [item.item_id]) == {
        item.item_id: warehouse.warehouse_ref1c
    }


def test_same_snapshot_is_storage_noop(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    observed = datetime.now(timezone.utc)
    kwargs = {
        "positions": [{"name": "same", "item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1}],
        "warehouses": [{"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c}],
    }
    first = publish_prodflow_feeder_assignments(db_session, observed_at=observed, **kwargs)
    db_session.flush()
    row = db_session.query(models.ProdFlowFeederAssignment).one()
    row_id = row.id
    loaded_at = db_session.get(models.ProdFlowFeederManifest, 1).loaded_at

    second = publish_prodflow_feeder_assignments(
        db_session, observed_at=observed + timedelta(days=1), **kwargs
    )
    db_session.flush()

    assert first["changed"] is True
    assert second["changed"] is False
    assert db_session.query(models.ProdFlowFeederAssignment).one().id == row_id
    assert db_session.get(models.ProdFlowFeederManifest, 1).loaded_at == loaded_at


@pytest.mark.parametrize("value", [None, "yes", 2])
def test_snapshot_rejects_invalid_active_flag(db_session, value):
    item, warehouse = _item_and_warehouse(db_session)
    with pytest.raises(ProdFlowFeederUnavailable, match="is_active is invalid"):
        publish_prodflow_feeder_assignments(
            db_session,
            positions=[{"item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": value}],
            warehouses=[{"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c}],
            observed_at=datetime.now(timezone.utc),
        )


def test_empty_snapshot_and_conflicting_warehouse_rows_preserve_old_good(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    observed = datetime.now(timezone.utc)
    first = publish_prodflow_feeder_assignments(
        db_session,
        positions=[{"item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1}],
        warehouses=[{"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c}],
        observed_at=observed,
    )
    db_session.commit()
    with pytest.raises(ProdFlowFeederUnavailable, match="no active assignments"):
        publish_prodflow_feeder_assignments(
            db_session, positions=[], warehouses=[], observed_at=observed
        )
    with pytest.raises(ProdFlowFeederUnavailable, match="conflicting custom_ref1c"):
        publish_prodflow_feeder_assignments(
            db_session,
            positions=[{"item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1}],
            warehouses=[
                {"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c},
                {"name": warehouse.warehouse_name, "custom_ref1c": "other-ref"},
            ],
            observed_at=observed,
        )
    assert db_session.get(models.ProdFlowFeederManifest, 1).source_revision == first["source_revision"]


def test_same_snapshot_repairs_assignment_table_drift(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    observed = datetime.now(timezone.utc)
    kwargs = {
        "positions": [{"item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1}],
        "warehouses": [{"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c}],
    }
    publish_prodflow_feeder_assignments(db_session, observed_at=observed, **kwargs)
    db_session.flush()
    db_session.query(models.ProdFlowFeederAssignment).delete(synchronize_session=False)
    db_session.flush()

    repaired = publish_prodflow_feeder_assignments(
        db_session, observed_at=observed + timedelta(minutes=1), **kwargs
    )
    db_session.flush()

    assert repaired["changed"] is True
    assert db_session.query(models.ProdFlowFeederAssignment).count() == 1


def test_same_snapshot_removes_extra_wrong_revision_row(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    extra = models.Item(item_code="PF-EXTRA", item_name="Extra")
    db_session.add(extra)
    db_session.flush()
    observed = datetime.now(timezone.utc)
    kwargs = {
        "positions": [{"item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1}],
        "warehouses": [{"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c}],
    }
    publish_prodflow_feeder_assignments(db_session, observed_at=observed, **kwargs)
    db_session.flush()
    db_session.add(models.ProdFlowFeederAssignment(
        item_id=extra.item_id,
        warehouse_ref1c=warehouse.warehouse_ref1c,
        source_item_code=extra.item_code,
        source_revision="stale-revision",
    ))
    db_session.flush()

    repaired = publish_prodflow_feeder_assignments(
        db_session, observed_at=observed + timedelta(minutes=1), **kwargs
    )
    db_session.flush()

    assert repaired["changed"] is True
    assert db_session.query(models.ProdFlowFeederAssignment).count() == 1


def test_snapshot_deduplicates_equivalent_warehouse_rows_and_aliases(db_session):
    item, warehouse = _item_and_warehouse(db_session)
    position = [{"item": item.item_code, "warehouse": warehouse.warehouse_name, "is_active": 1}]
    result = publish_prodflow_feeder_assignments(
        db_session,
        positions=position,
        warehouses=[
            {"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c},
            {"name": warehouse.warehouse_name, "custom_ref1c": warehouse.warehouse_ref1c},
            {"name": "Alias", "custom_ref1c": warehouse.warehouse_ref1c},
        ],
        observed_at=datetime.now(timezone.utc),
    )
    assert result["rows"] == 1
