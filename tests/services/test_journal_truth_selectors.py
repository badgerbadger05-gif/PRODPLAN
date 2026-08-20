"""Journal read scopes must follow the published Ledger pointer, never run id."""

from datetime import date, datetime, timezone

import pytest

from app import models
from app.services.planning_truth import PlanningTruthUnavailable
from app.services.production_control_journal import list_journal as production_journal
from app.services.purchase_control_journal import list_journal as purchase_journal
from app.services.purchase_control_snapshot import PurchaseJournalSnapshotUnavailable


def _generation(db, key: str):
    cutoff = datetime(2026, 7, 23, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"physical:{key}", status="completed", cutoff=cutoff,
        completed_at=cutoff, source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=key, status="accepted", cutoff=cutoff, accepted_at=cutoff,
        source_watermarks={}, capabilities={}, physical_import_batch=physical,
        algorithm_version="tests/1",
    )
    db.add(generation)
    db.flush()
    return generation


def _plan_and_run(db, *, generation, name: str, status: str = "fixed"):
    plan = models.ProductionPlanHeader(
        name=name, period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status=status,
    )
    db.add(plan)
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to,
        ledger_generation_id=generation.id,
    )
    db.add(run)
    db.flush()
    return plan, run


def test_purchase_journal_uses_only_exact_published_generation_and_fixed_plan(db_session):
    current = _generation(db_session, "current-purchase")
    foreign = _generation(db_session, "foreign-purchase")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=current.id))
    item = models.Item(item_code="J-TRUTH-P", item_name="Journal truth purchase", unit="шт")
    supplier = models.Supplier(supplier_ref1c="supplier-jtruth", supplier_name="Supplier")
    db_session.add_all([item, supplier])
    db_session.flush()

    _, exact = _plan_and_run(db_session, generation=current, name="exact")
    _, foreign_run = _plan_and_run(db_session, generation=foreign, name="foreign")
    _, draft_run = _plan_and_run(db_session, generation=current, name="draft", status="draft")
    for run, qty in ((exact, 11), (foreign_run, 22), (draft_run, 33)):
        db_session.add(models.PlannedPurchase(
            run_id=run.run_id, item_id=item.item_id, requested_qty=qty, planned_qty=qty, qty=qty,
            need_date=date(2026, 8, 20), order_date=date(2026, 8, 10), lead_time_days=10,
            bucket_date=date(2026, 8, 20), supplier_ref1c=supplier.supplier_ref1c,
            ledger_generation_id=run.ledger_generation_id,
        ))
    current.capabilities = {
        "physical_ledger": True,
        "reservation_replay": True,
        "planning_snapshots": True,
        "purchase_control_journal": True,
    }
    db_session.add(models.PlanningReadSnapshot(
        consumer="purchase_control_journal",
        snapshot_key="journal:v1",
        ledger_generation_id=current.id,
        cutoff=current.cutoff,
        truth_status="accepted",
        reason=None,
        payload={
            "meta": {
                "ledger_generation_id": current.id,
                "fact_source": "ledger",
                "read_only": True,
            },
            "rows": [],
            "cards": {},
        },
        published_at=current.accepted_at,
    ))
    db_session.commit()

    result = purchase_journal(db_session, today=date(2026, 7, 23))
    assert result["rows"] == []
    assert result["run_ids"] == []
    assert result["ledger_generation_id"] == current.id


def test_purchase_journal_is_explicitly_unavailable_without_published_pointer(db_session):
    with pytest.raises(PurchaseJournalSnapshotUnavailable, match="No Item Ledger generation"):
        purchase_journal(db_session)


def test_production_root_scope_uses_exact_generation_and_fixed_plan(db_session):
    current = _generation(db_session, "current-production")
    foreign = _generation(db_session, "foreign-production")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=current.id))
    root = models.Item(item_code="J-TRUTH-ROOT", item_name="Root", unit="шт")
    component = models.Item(item_code="J-TRUTH-COMP", item_name="Component", unit="шт")
    db_session.add_all([root, component])
    db_session.flush()
    spec = models.Specification(spec_name="truth selector spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add_all([
        models.DefaultSpecification(item_id=root.item_id, spec_id=spec.spec_id),
        models.SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1),
    ])

    exact_plan, exact = _plan_and_run(db_session, generation=current, name="exact")
    foreign_plan, foreign_run = _plan_and_run(db_session, generation=foreign, name="foreign")
    draft_plan, draft_run = _plan_and_run(db_session, generation=current, name="draft", status="draft")
    for plan in (exact_plan, foreign_plan, draft_plan):
        db_session.add(models.ProductionPlanLine(
            plan_id=plan.id, item_id=root.item_id, bucket_date=date(2026, 8, 1), qty=1,
        ))
    for run, number in ((exact, "EXACT"), (foreign_run, "FOREIGN"), (draft_run, "DRAFT")):
        order = models.ProductionOrder(
            order_number=number, order_date=datetime(2026, 7, 23), is_posted=True,
            deletion_mark=False, source="mrp", source_run_id=run.run_id,
        )
        db_session.add(order)
        db_session.flush()
        product = models.ProductionProduct(
            order_id=order.order_id, item_id=component.item_id, line_number=1,
            quantity=1, produced_qty=0, remaining_qty=1,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(models.ProductionOrderLineState(product_id=product.product_id, status="shortage"))
    db_session.commit()

    result = production_journal(db_session, root_item_id=root.item_id)
    assert [row["order_number"] for row in result["rows"]] == ["EXACT"]
    assert result["latest_run_id"] == exact.run_id

    # The unfiltered journal must not leak a newer fixed foreign-generation
    # MRP order either; the root selector is not the truth boundary.
    unfiltered = production_journal(db_session)
    assert [row["order_number"] for row in unfiltered["rows"]] == ["EXACT"]


def test_production_journal_is_explicitly_unavailable_without_published_pointer(db_session):
    with pytest.raises(PlanningTruthUnavailable, match="No Item Ledger generation"):
        production_journal(db_session)


def test_order_opened_in_1c_survives_the_retirement_of_its_run(db_session):
    """A launched order is an executive fact, not a projection of a live run.

    A specification rebase or a re-fixed plan retires the run an order was
    launched from within the hour.  Filtering by run scope then erased real,
    opened 1C orders from the journal: the row disappeared, its route sheet
    could not be printed because the published snapshot no longer carried the
    product, and the demand it was covering was offered for launch again.
    """
    current = _generation(db_session, "current-launched")
    retired = _generation(db_session, "retired-launched")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=current.id))
    item = models.Item(item_code="J-LAUNCHED", item_name="Launched part", unit="шт")
    db_session.add(item)
    db_session.flush()
    _live_plan, live_run = _plan_and_run(db_session, generation=current, name="live")
    _old_plan, retired_run = _plan_and_run(db_session, generation=retired, name="retired")

    for number, run, ref1c in (
        ("LAUNCHED", retired_run, "6f1f5690-5345-11f1-9dae-9ee51454587f"),
        ("PROPOSED", retired_run, None),
        ("LIVE", live_run, None),
    ):
        order = models.ProductionOrder(
            order_number=number, order_date=datetime(2026, 7, 23), is_posted=True,
            deletion_mark=False, source="mrp", source_run_id=run.run_id,
            order_ref1c=ref1c,
        )
        db_session.add(order)
        db_session.flush()
        product = models.ProductionProduct(
            order_id=order.order_id, item_id=item.item_id, line_number=1,
            quantity=15, produced_qty=0, remaining_qty=15,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(
            models.ProductionOrderLineState(product_id=product.product_id, status="shortage")
        )
    db_session.commit()

    rows = production_journal(db_session)["rows"]

    # The opened 1C order stays even though its run and plan are both retired.
    # The local order of that foreign plan does not: it is not this
    # generation's work at all.
    assert sorted(row["order_number"] for row in rows) == ["LAUNCHED", "LIVE"]


def test_local_order_of_a_rebased_run_stays_visible_within_its_plan(db_session):
    """What nets the launch quantity has to be visible.

    A rebase retires the run but keeps the plan, and the order materialized
    before it is still that plan's work — its material issues and route sheet
    are real, and its open quantity is now subtracted from what may be launched.
    Hiding it while it silently reduces the proposal is the worst of both
    worlds.
    """
    current = _generation(db_session, "current-rebased")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=current.id))
    item = models.Item(item_code="J-REBASED", item_name="Rebased part", unit="шт")
    db_session.add(item)
    db_session.flush()
    plan, live_run = _plan_and_run(db_session, generation=current, name="live plan")
    retired_run = models.PlanningRun(
        status="CLOSED", config_snapshot={}, source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to,
        ledger_generation_id=current.id,
    )
    db_session.add(retired_run)
    db_session.flush()

    for number, run in (("BEFORE-REBASE", retired_run), ("AFTER-REBASE", live_run)):
        order = models.ProductionOrder(
            order_number=number, order_date=datetime(2026, 7, 23), is_posted=True,
            deletion_mark=False, source="mrp", source_run_id=run.run_id,
        )
        db_session.add(order)
        db_session.flush()
        product = models.ProductionProduct(
            order_id=order.order_id, item_id=item.item_id, line_number=1,
            quantity=4, produced_qty=0, remaining_qty=4,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(
            models.ProductionOrderLineState(product_id=product.product_id, status="shortage")
        )
    db_session.commit()

    rows = production_journal(db_session)["rows"]

    assert sorted(row["order_number"] for row in rows) == [
        "AFTER-REBASE", "BEFORE-REBASE",
    ]
