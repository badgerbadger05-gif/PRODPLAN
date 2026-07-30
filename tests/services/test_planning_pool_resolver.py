import pytest

from app import models
from app.services.planning_pool_resolver import (
    PlanningPoolConfigurationError,
    resolve_planning_pool_by_warehouse,
)


def test_live_planning_pool_uses_only_selected_nonfinished_nonignored_warehouses(
    db_session,
):
    db_session.add_all(
        [
            models.StockWarehouse(
                warehouse_ref1c="WH-LIVE",
                warehouse_name="Live",
                is_selected=True,
                is_finished_goods=False,
            ),
            models.StockWarehouse(
                warehouse_ref1c="WH-FINISHED",
                warehouse_name="Finished",
                is_selected=True,
                is_finished_goods=True,
            ),
            models.StockWarehouse(
                warehouse_ref1c="WH-IGNORED",
                warehouse_name="Ignored",
                is_selected=True,
                is_finished_goods=False,
            ),
            models.StockWarehouse(
                warehouse_ref1c="WH-OFF",
                warehouse_name="Off",
                is_selected=False,
                is_finished_goods=False,
            ),
            models.IgnoredWarehouse(warehouse_ref1c="WH-IGNORED"),
        ]
    )
    db_session.flush()

    assert resolve_planning_pool_by_warehouse(db_session) == {
        "WH-LIVE": "default"
    }


def test_empty_live_planning_pool_fails_closed(db_session):
    db_session.add(
        models.StockWarehouse(
            warehouse_ref1c="WH-FINISHED",
            warehouse_name="Finished",
            is_selected=True,
            is_finished_goods=True,
        )
    )
    db_session.flush()

    with pytest.raises(
        PlanningPoolConfigurationError,
        match="planning warehouse contour is empty",
    ):
        resolve_planning_pool_by_warehouse(db_session)
