"""R6 PostgreSQL MVCC checks against the named local contour only."""

from __future__ import annotations

import os

import pytest


def _dsn() -> str:
    value = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not value:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(value)
    return value


@pytest.mark.integration
def test_current_stock_mvcc_keeps_previous_value_until_commit():
    dsn = _dsn()
    sa = pytest.importorskip("sqlalchemy")
    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    left = engine.connect()
    right = engine.connect()
    try:
        fixture = right.execute(sa.text(
            "SELECT (SELECT item_id FROM items ORDER BY item_id LIMIT 1) AS item_id, "
            "(SELECT id FROM ledger_generation WHERE status = 'accepted' ORDER BY id LIMIT 1) AS generation_id"
        )).first()
        if fixture is None or fixture.item_id is None or fixture.generation_id is None:
            pytest.fail("local R2 contour lacks an item and accepted generation for the R6 MVCC fixture")
        with left.begin():
            row_id = left.execute(sa.text(
                "INSERT INTO stock_bin (ledger_generation_id, item_id, characteristic_ref, organization_ref, "
                "warehouse_ref1c, on_hand, reconcile_pending_qty, is_current) "
                "VALUES (:generation, :item, '', 'r6-test-org', 'r6-test-wh', 10, 0, true) RETURNING id"
            ), {"generation": int(fixture.generation_id), "item": int(fixture.item_id)}).scalar_one()
        row = right.execute(sa.text(
            "SELECT id, on_hand FROM stock_bin WHERE id = :id"
        ), {"id": int(row_id)}).first()
        assert row is not None
        tx = left.begin()
        left.execute(sa.text(
            "UPDATE stock_bin SET on_hand = on_hand + 1 WHERE id = :id"
        ), {"id": int(row_id)})
        # A separate READ COMMITTED session sees the accepted value, not the
        # uncommitted candidate.  Rollback leaves the named contour unchanged.
        observed = right.execute(sa.text(
            "SELECT on_hand FROM stock_bin WHERE id = :id"
        ), {"id": int(row_id)}).scalar_one()
        assert str(observed) == str(row.on_hand)
        tx.rollback()
        with left.begin():
            left.execute(sa.text("DELETE FROM stock_bin WHERE id = :id"), {"id": int(row_id)})
    finally:
        left.close()
        right.close()
        engine.dispose()
