"""GET /purchase-control/orders keeps one canonical journal contract.

The current-execution path and the canonical reader must answer with the same
materialization rule, the same page totals and the same read-time truth.  A
second copy of any of them is exactly the parallel-engine defect CANON.md
forbids, and the operator loses the ability to create supplier orders when the
action flag silently disappears from the transport.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app import models
from app.routers.purchase_control import get_orders
from app.services.item_ledger import current_execution
from app.services.item_ledger.current_execution import (
    publish_current_purchase_control_from_payload,
)
from app.services.purchase_control_journal import list_journal


_SUMMARY_KEYS = {
    "total_rows",
    "by_status",
    "by_phase",
    "to_order",
    "overdue",
    "expected_7d",
    "in_transit_amount",
    "fact_status",
}


def _buy_row(key="buy:1", *, to_order=6.0, line_status="to_order"):
    return {
        "row_generator": "mrp_reservation",
        "row_key": key,
        "line_status": line_status,
        "supply_phase": "no_goods",
        "required_qty": 12.0,
        "realized_qty": 4.0,
        "received_qty": 4.0,
        "open_order_covered_qty": 2.0,
        "to_order_qty": to_order,
        "quantity": 12.0,
        "remaining_qty": 6.0,
        "delivery_date": "2026-08-31",
        "reservation_ids": [1],
        "requirement_ids": [1],
    }


def _supplier_row(key="order:1"):
    return {
        "row_generator": "supplier_order",
        "row_key": key,
        "line_status": "expected",
        "supply_phase": "in_transit",
        "quantity": 5.0,
        "received_qty": 2.0,
        "remaining_qty": 3.0,
        "delivery_date": "2026-09-30",
        "order_id": 77,
        "supplier_id": 3,
        "supplier_name": "ООО Поставщик",
    }


def _manifest(summary):
    return SimpleNamespace(
        id=17,
        source_generation_id=42,
        source_revision="accepted:g42:purchase_control_journal",
        summary=summary,
    )


def _current_rows(*rows):
    return [
        SimpleNamespace(
            business_identity=f"purchase:{index}",
            source_revision="accepted:g42",
            payload=dict(row),
        )
        for index, row in enumerate(rows, 1)
    ]


def _build_time_envelope():
    """Publication envelope exactly as the publisher persists it."""
    return {
        "meta": {
            "truth_status": "building",
            "ledger_generation_id": 42,
            "cutoff": "2026-07-28T12:00:00+00:00",
            "run_ids": [5],
            "to_order_by_period": [],
            "received_qty_status": "available",
            "fact_source": "item_ledger",
            "read_only": True,
        },
        "summary": {"to_order": 327, "total_rows": 874, "fact_status": "available"},
        "cards": {},
        "total_rows": 874,
    }


def test_current_rows_carry_the_canonical_materialization_action(monkeypatch):
    manifest = _manifest(_build_time_envelope())
    monkeypatch.setattr(
        current_execution, "require_current_execution_scope", lambda *a, **k: manifest
    )
    monkeypatch.setattr(
        current_execution,
        "load_current_execution_rows",
        lambda *a, **k: _current_rows(_buy_row(), _supplier_row()),
    )

    result = get_orders(db=object(), horizon_period_to=None, limit=100, offset=0)
    rows = {row["row_key"]: row for row in result["rows"]}

    assert rows["buy:1"]["can_materialize"] is True
    assert rows["buy:1"]["materialize_disabled_reason"] is None
    assert rows["order:1"]["can_materialize"] is False
    assert rows["order:1"]["materialize_disabled_reason"] == "Строка не является MRP-снабжением"


def test_materialization_action_is_stamped_on_the_projected_row(monkeypatch):
    """A BUY row whose only slice is outside the horizon is not orderable."""
    buy = _buy_row()
    buy["slices"] = [
        {
            "plan_period_from": "2026-08-01",
            "plan_period_to": "2026-08-31",
            "period_label": "Август 2026",
            "required_qty": 12.0,
            "realized_qty": 4.0,
            "open_order_covered_qty": 8.0,
            "to_order_qty": 0.0,
            "run_id": 5,
            "requirement_id": 1,
            "reservation_id": 1,
        }
    ]
    manifest = _manifest(_build_time_envelope())
    monkeypatch.setattr(
        current_execution, "require_current_execution_scope", lambda *a, **k: manifest
    )
    monkeypatch.setattr(
        current_execution, "load_current_execution_rows", lambda *a, **k: _current_rows(buy)
    )

    from datetime import date

    result = get_orders(
        db=object(), horizon_period_to=date(2026, 8, 31), active_only=False,
        limit=100, offset=0,
    )
    row = result["rows"][0]

    assert row["line_status"] == "expected"
    assert row["can_materialize"] is False
    assert row["materialize_disabled_reason"] == "Строка не требует нового заказа"


def test_summary_counts_the_served_rows_not_the_build_envelope(monkeypatch):
    manifest = _manifest(_build_time_envelope())
    monkeypatch.setattr(
        current_execution, "require_current_execution_scope", lambda *a, **k: manifest
    )
    monkeypatch.setattr(
        current_execution,
        "load_current_execution_rows",
        lambda *a, **k: _current_rows(_buy_row(), _supplier_row()),
    )

    result = get_orders(db=object(), horizon_period_to=None, limit=100, offset=0)
    summary = result["summary"]

    assert set(summary) == _SUMMARY_KEYS
    assert summary["total_rows"] == result["total"] == len(result["rows"]) == 2
    assert summary["by_status"] == {"to_order": 1, "expected": 1}
    assert summary["by_phase"] == {"no_goods": 1, "in_transit": 1}
    assert summary["to_order"] == 1
    assert summary["overdue"] == 0
    assert summary["fact_status"] == "available"


def test_summary_follows_the_applied_filter(monkeypatch):
    manifest = _manifest(_build_time_envelope())
    monkeypatch.setattr(
        current_execution, "require_current_execution_scope", lambda *a, **k: manifest
    )
    monkeypatch.setattr(
        current_execution,
        "load_current_execution_rows",
        lambda *a, **k: _current_rows(_buy_row(), _supplier_row()),
    )

    result = get_orders(
        db=object(), line_status="to_order", horizon_period_to=None, limit=100, offset=0,
    )

    assert result["summary"]["total_rows"] == 1
    assert result["summary"]["by_status"] == {"to_order": 1}


def test_served_scope_reports_accepted_truth_not_the_stored_building_status(monkeypatch):
    envelope = _build_time_envelope()
    assert envelope["meta"]["truth_status"] == "building"
    manifest = _manifest(envelope)
    monkeypatch.setattr(
        current_execution, "require_current_execution_scope", lambda *a, **k: manifest
    )
    monkeypatch.setattr(
        current_execution, "load_current_execution_rows", lambda *a, **k: _current_rows(_buy_row())
    )

    result = get_orders(db=object(), horizon_period_to=None, limit=100, offset=0)

    assert result["truth_status"] == "accepted"
    assert result["meta"]["truth_status"] == "accepted"
    assert result["meta"]["truth_reason"] is None
    assert result["meta"]["ledger_generation"] == 42
    assert result["ledger_generation_id"] == 42
    assert result["meta"]["current_scope_id"] == 17
    # Candidate-only transport stays available to the frontend.
    assert result["rows"][0]["current_identity"] == "purchase:1"
    assert result["rows"][0]["source_revision"] == manifest.source_revision


def _accepted_generation(db_session):
    cutoff = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="purchase-contract-physical", status="completed",
        cutoff=cutoff, source_watermarks={}, completed_at=cutoff,
    )
    generation = models.LedgerGeneration(
        physical_import_batch=physical, generation_key="purchase-contract-generation",
        status="accepted", cutoff=cutoff, accepted_at=cutoff,
        source_watermarks={}, capabilities={}, algorithm_version="tests/purchase-contract",
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def test_current_path_is_a_superset_of_the_canonical_reader_contract(db_session):
    generation = _accepted_generation(db_session)
    envelope = _build_time_envelope()
    publish_current_purchase_control_from_payload(
        db_session,
        generation.id,
        {
            "meta": envelope["meta"],
            "summary": envelope["summary"],
            "cards": {},
            "rows": [
                {"current_identity": "purchase:1", "payload": _buy_row()},
                {"current_identity": "purchase:2", "payload": _supplier_row()},
            ],
        },
    )

    current = get_orders(db=db_session, horizon_period_to=None, limit=100, offset=0)
    canonical = list_journal(db_session)

    assert set(canonical["summary"]) <= set(current["summary"])
    assert canonical["summary"] == current["summary"]
    assert set(canonical["meta"]) <= set(current["meta"])
    assert current["truth_status"] == canonical["truth_status"] == "accepted"

    canonical_rows = {row["row_key"]: row for row in canonical["rows"]}
    for row in current["rows"]:
        expected = canonical_rows[row["row_key"]]
        assert set(expected) <= set(row)
        assert row["can_materialize"] == expected["can_materialize"]
        assert row["materialize_disabled_reason"] == expected["materialize_disabled_reason"]


def _ledger_supply_row(
    key="ledger-supply:supplier_order:REF:1:supplier_order_item:9",
    *,
    delivery_date,
    line_status="expected",
    overdue_days=0,
):
    """A supplier row exactly as the current publisher persists it."""
    return {
        "row_generator": "ledger_future_supply",
        "row_key": key,
        "line_status": line_status,
        "overdue_days": overdue_days,
        "supply_phase": "in_transit",
        "quantity": 5.0,
        "received_qty": 0.0,
        "remaining_qty": 5.0,
        "delivery_date": delivery_date,
        "order_id": 77,
        "order_number": "ЗСНФ-001727",
        "order_ref1c": "REF",
        "order_state_name": "Заказан (товар в пути)",
        "item_code": "ITEM-1",
        "supplier_id": 3,
        "supplier_name": "ООО Поставщик",
        "fact_status": "available",
        "fact_source": "ledger",
    }


def test_current_path_evaluates_the_calendar_status_for_the_serving_day(monkeypatch):
    """The served endpoint, not only the reader facade, answers for today.

    A current row survives many bounded refreshes untouched, so a stored
    ``expected`` with a delivery date that has since passed would otherwise
    keep the line out of the ``overdue`` filter and out of the page total.
    """
    from datetime import date, timedelta

    passed = date.today() - timedelta(days=8)
    manifest = _manifest(_build_time_envelope())
    monkeypatch.setattr(
        current_execution, "require_current_execution_scope", lambda *a, **k: manifest
    )
    monkeypatch.setattr(
        current_execution,
        "load_current_execution_rows",
        lambda *a, **k: _current_rows(
            _ledger_supply_row(delivery_date=passed.isoformat())
        ),
    )

    result = get_orders(db=object(), horizon_period_to=None, limit=100, offset=0)

    assert [row["line_status"] for row in result["rows"]] == ["overdue"]
    assert [row["overdue_days"] for row in result["rows"]] == [8]
    assert result["summary"]["by_status"] == {"overdue": 1}
    assert result["summary"]["overdue"] == 1

    filtered = get_orders(
        db=object(), horizon_period_to=None, line_status="overdue",
        limit=100, offset=0,
    )
    assert filtered["total"] == 1
    stale_filter = get_orders(
        db=object(), horizon_period_to=None, line_status="expected",
        limit=100, offset=0,
    )
    assert stale_filter["total"] == 0
