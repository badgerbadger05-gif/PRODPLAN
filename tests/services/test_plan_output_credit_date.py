from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.item_ledger.assembly_queue_materialization import plan_output_eligible_from
from app.services.item_ledger.assembly_output_core import OutputFact, QueueCandidate, allocate_output_fact
from app.services.period_plan_service import create_period_plan, update_period_plan_header


@pytest.mark.parametrize("instant, expected", [
    ("2026-11-16T20:59:59+00:00", Decimal("0")),
    ("2026-11-16T21:00:00+00:00", Decimal("1")),
    ("2026-11-17T08:00:00+03:00", Decimal("1")),
])
def test_explicit_date_filters_output_at_moscow_midnight(instant, expected):
    plan = SimpleNamespace(output_credit_from=date(2026, 11, 17), fixed_at=datetime(2026, 11, 16, 12))
    candidate = QueueCandidate(1, 1, 1, Decimal("10"), eligible_from=plan_output_eligible_from(plan))
    fact = OutputFact(1, 1, Decimal("1"), posting_at=datetime.fromisoformat(instant))
    result = allocate_output_fact(fact, [candidate])
    assert sum((r.qty for r in result.allocations), Decimal("0")) == expected


def test_existing_plan_boundary_is_unchanged():
    fixed = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    assert plan_output_eligible_from(SimpleNamespace(fixed_at=fixed)) is fixed


def test_create_persists_date_without_falsifying_fixation(db_session):
    result = create_period_plan(db_session, name="Consolidated", period_from="2026-11-17",
                               period_to="2026-12-31", output_credit_from="2026-11-17")
    assert result["output_credit_from"] == "2026-11-17"
    assert result["fixed_at"] is None
    assert result["status"] == "draft"


def test_credit_date_must_be_in_plan_period(db_session):
    with pytest.raises(ValueError, match="зачёта"):
        create_period_plan(db_session, name="Invalid", period_from="2026-11-17",
                           period_to="2026-12-31", output_credit_from="2026-11-16")


def test_credit_date_can_only_be_changed_on_draft(db_session):
    from app.models import ProductionPlanHeader
    result = create_period_plan(db_session, name="Draft", period_from="2026-11-17", period_to="2026-12-31")
    updated = update_period_plan_header(db_session, result["id"], output_credit_from="2026-11-17")
    assert updated["output_credit_from"] == "2026-11-17"
    with pytest.raises(ValueError, match="зачёта"):
        update_period_plan_header(db_session, result["id"], period_from="2026-11-18")
    db_session.rollback()
    plan = db_session.get(ProductionPlanHeader, result["id"])
    plan.status = "fixed"
    db_session.commit()
    with pytest.raises(ValueError):
        update_period_plan_header(db_session, result["id"], output_credit_from="2026-11-18")


def test_matrix_keeps_non_friday_period_end(db_session, monkeypatch):
    from app.models import Item
    from app.services import period_plan_service as service
    item = Item(item_code="CREDIT-DATE", item_name="Credit date", unit="шт")
    db_session.add(item)
    db_session.flush()
    plan = service.create_period_plan(db_session, name="December", period_from="2026-11-17", period_to="2026-12-31")
    service.bulk_upsert_period_plan_lines(db_session, plan["id"], [dict(item_id=item.item_id, bucket_date="2026-12-31", qty=1275)])
    monkeypatch.setattr(service, "_plan_matrix_forecasts", lambda *args: {})
    matrix = service.get_period_plan_matrix(db_session, plan["id"])
    assert matrix["buckets"][-1] == "2026-12-31"
    assert matrix["bucket_totals"]["2026-12-31"] == matrix["grand_total"] == 1275
