from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import models
from app.routers.production_control import get_orders_journal
from app.services import production_control_material_availability as material_availability
from app.services.planning_truth import publish_generation
from app.services.production_control_material_availability import get_materials_snapshot
from app.services.production_material_custody_projection import (
    initialize_material_custody_baseline,
)
from app.services.production_control_journal_snapshot import (
    ProductionControlJournalPromotionError,
    ProductionControlJournalSnapshotUnavailable,
    RouteSheetSnapshotUnavailable,
    list_root_product_options,
    _public_journal_row,
    build_candidate_snapshot,
    read_route_sheet_snapshot_rows,
    promote_candidate_snapshot,
    validate_candidate_snapshot,
    read_snapshot,
)
from app.services.item_ledger.future_supply_capture import (
    FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
    replace_future_supply_capture,
)
from app.services.production_control_common import DONE_STATE_KEY


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "execution_allocations": True,
    "planning_snapshots": True,
    "production_control_journal": True,
    "future_supply": True,
}


def test_public_journal_row_strips_internal_material_snapshot():
    source = {
        "product_id": 7,
        "material_coverage_snapshot": {"components": [{"item_id": 9}]},
        "_route_sheet_snapshot": {"version": 1},
    }

    assert _public_journal_row(source) == {"product_id": 7}
    assert "material_coverage_snapshot" in source
    assert "_route_sheet_snapshot" in source


def _building_generation(db, key: str):
    cutoff = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"{key}:physical",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
    )
    generation = models.LedgerGeneration(
        generation_key=key,
        status="building",
        cutoff=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
        replay_version="test",
    )
    db.add(generation)
    db.flush()
    _seed_future_supply_capture(db, generation)
    initialize_material_custody_baseline(
        db,
        ledger_generation_id=int(generation.id),
        cells=[],
        observed_at=generation.cutoff,
    )
    db.expire_all()
    return generation


def _seed_future_supply_capture(db, generation):
    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage="future_supply_capture",
        batch_key=f"{generation.id}:future_supply_capture",
        status="building",
        algorithm_version=FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
        metrics={},
    )
    db.add(batch)
    db.flush()
    replace_future_supply_capture(
        db,
        int(generation.id),
        int(batch.id),
        [],
    )
    batch.status = "completed"
    batch.completed_at = generation.cutoff
    db.flush()




def _journal_line(db):
    item = models.Item(
        item_code="SNAP-PROD-1",
        item_name="Snapshot production line",
        item_article="SNAP-ARTICLE",
        unit="шт",
        status="active",
    )
    order = models.ProductionOrder(
        order_number="SNAP-ORDER-1",
        order_date=datetime(2026, 7, 20),
        source="1c",
        deletion_mark=False,
    )
    db.add_all([item, order])
    db.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=10,
        produced_qty=3,
        remaining_qty=999,
    )
    db.add(product)
    db.flush()
    return item, order, product


def _make_proposal(db, generation):
    item = models.Item(
        item_code="SNAP-MAKE-PROPOSAL",
        item_name="Snapshot MAKE proposal",
        item_article="SNAP-MAKE",
        unit="шт",
        replenishment_method="Производство",
        status="active",
    )
    component = models.Item(
        item_code="SNAP-MAKE-COMPONENT",
        item_name="Snapshot MAKE component",
        item_article="SNAP-COMP",
        unit="шт",
        replenishment_method="Закупка",
        status="active",
    )
    spec = models.Specification(
        spec_code="SNAP-MAKE-SPEC",
        spec_name="Snapshot MAKE specification",
        spec_ref1c="snap-make-spec",
    )
    plan = models.ProductionPlanHeader(
        name="Snapshot proposal plan",
        period_from=generation.cutoff.date(),
        period_to=generation.cutoff.date(),
        status="fixed",
    )
    db.add_all([item, component, spec, plan])
    db.flush()
    db.add_all([
        models.DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id),
        models.SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1),
    ])
    db.flush()
    plan_line = models.ProductionPlanLine(
        plan_id=plan.id,
        item_id=item.item_id,
        bucket_date=generation.cutoff.date(),
        qty=12,
    )
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db.add_all([plan_line, run])
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=12,
        net_required_qty=12,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
        freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="make",
        reserved_qty=12,
        covered_from_stock_at_freeze_qty=0,
        replenishment_required_qty=12,
        replenishment_received_qty=2,
        realized_qty=2,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    work = models.ReplenishmentWorkItem(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        plan_id=plan.id,
        run_id=run.run_id,
        requirement_id=requirement.id,
        item_id=item.item_id,
        replenishment_method="make",
        replenishment_required_qty=12,
        replenishment_fulfilled_qty=2,
        replenishment_remaining_qty=10,
    )
    db.add(work)
    db.flush()
    return run, work


def _accept(db, generation, snapshot):
    accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    generation.status = "accepted"
    generation.accepted_at = accepted_at
    generation.capabilities = dict(CAPABILITIES)
    promoted = promote_candidate_snapshot(
        db,
        generation=generation,
        accepted_at=accepted_at,
    )
    assert promoted is snapshot
    publish_generation(db, generation)
    db.commit()


def _setup_chain_journal_rows(db):
    painted = models.Item(
        item_code="SNAP-PAINTED",
        item_name="Snap painted",
        item_article="SNAP-P",
        unit="шт",
        status="active",
    )
    welded = models.Item(
        item_code="SNAP-WELDED",
        item_name="Snap welded",
        item_article="SNAP-W",
        unit="шт",
        status="active",
    )
    db.add_all([painted, welded])
    db.flush()

    paint_order = models.ProductionOrder(
        order_number="SNAP-PAINT-ORDER",
        order_date=datetime(2026, 7, 20),
        deletion_mark=False,
        source="1c",
    )
    weld_order = models.ProductionOrder(
        order_number="SNAP-WELD-ORDER",
        order_date=datetime(2026, 7, 20),
        deletion_mark=False,
        source="1c",
    )
    db.add_all([paint_order, weld_order])
    db.flush()

    paint_product = models.ProductionProduct(
        order_id=paint_order.order_id,
        item_id=painted.item_id,
        line_number=1,
        quantity=8,
        produced_qty=0,
        remaining_qty=8,
    )
    weld_product = models.ProductionProduct(
        order_id=weld_order.order_id,
        item_id=welded.item_id,
        line_number=1,
        quantity=8,
        produced_qty=0,
        remaining_qty=6,
    )
    db.add_all([paint_product, weld_product])
    db.flush()

    pair = models.PaintWeldPair(
        painted_item_id=painted.item_id,
        welded_item_id=welded.item_id,
        source="auto",
    )
    db.add(pair)
    db.flush()
    db.add(
        models.PaintWeldChainLink(
            painted_order_id=paint_order.order_id,
            welded_order_id=weld_order.order_id,
            pair_id=pair.id,
        )
    )
    db.flush()
    return paint_product, weld_product


def test_candidate_snapshot_row_contains_route_sheet_snapshot(db_session):
    generation = _building_generation(db_session, "production-journal-row-route")
    item, order, product = _journal_line(db_session)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    db_session.flush()

    row = db_session.query(models.PlanningReadRow).filter_by(
        snapshot_id=snapshot.id,
        row_key=f"product:{product.product_id}",
    ).one()
    route_payload = row.payload["_route_sheet_snapshot"]
    assert int(route_payload["version"]) == 1
    assert int(route_payload["sheet"]["product_id"]) == product.product_id
    assert int(route_payload["sheet"]["remaining_qty"]) == 7
    assert "_route_sheet_snapshot" in row.payload


def test_candidate_snapshot_contains_unmaterialized_make_proposal(db_session):
    generation = _building_generation(db_session, "production-journal-make-proposal")
    run, work = _make_proposal(db_session, generation)

    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    row = db_session.query(models.PlanningReadRow).filter_by(
        snapshot_id=snapshot.id,
        row_key=f"work-item:{work.id}",
    ).one()

    assert row.row_kind == "production_proposal"
    assert row.payload["journal_row_key"] == f"work-item:{work.id}"
    assert row.payload["work_item_id"] == work.id
    assert row.payload["product_id"] is None
    assert row.payload["order_id"] is None
    assert row.payload["remaining_qty"] == 10
    assert row.payload["status"] == "not_created"
    assert row.payload["coverage_status"] == "shortage"
    assert row.payload["coverage_label"] == "Дефицит"
    assert row.payload["material_coverage_status"] == "shortage"
    assert row.payload["available_actions"] == ["materialize"]
    assert "_route_sheet_snapshot" not in row.payload
    assert db_session.query(models.ProductionOrder).count() == 0


def test_paint_weld_proposals_use_welded_frozen_bom_and_block_welded_row(db_session):
    generation = _building_generation(db_session, "production-journal-paint-weld")
    run, painted_work = _make_proposal(db_session, generation)
    painted_item = db_session.get(models.Item, int(painted_work.item_id))
    welded_item = models.Item(
        item_code="SNAP-WELDED-PROPOSAL",
        item_name="Сварная деталь",
        item_article="SNAP-WELDED",
        unit="шт",
        replenishment_method="Производство",
        status="active",
    )
    raw_item = models.Item(
        item_code="SNAP-WELD-RAW",
        item_name="Сырьё сварки",
        item_article="SNAP-RAW",
        unit="шт",
        replenishment_method="Закупка",
        status="active",
    )
    db_session.add_all([welded_item, raw_item])
    db_session.flush()
    db_session.add(
        models.PaintWeldPair(
            painted_item_id=int(painted_item.item_id),
            welded_item_id=int(welded_item.item_id),
            source="manual",
            is_active=True,
        )
    )
    db_session.add(
        models.MrpFreezeComponent(
            run_id=int(run.run_id),
            freeze_version=1,
            parent_item_id=int(welded_item.item_id),
            component_item_id=int(raw_item.item_id),
            spec_ref="frozen-weld-spec",
            spec_version="v1",
            norm_qty_per_unit=2,
            unit_coef=1,
        )
    )
    warehouse = models.StockWarehouse(
        warehouse_ref1c="paint-weld-stock",
        warehouse_code="PWS",
        warehouse_name="Paint weld stock",
        is_selected=True,
    )
    db_session.add(warehouse)
    db_session.flush()
    db_session.add(
        models.StockBin(
            ledger_generation_id=int(generation.id),
            item_id=int(raw_item.item_id),
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c=warehouse.warehouse_ref1c,
            on_hand=20,
        )
    )

    welded_requirement = models.MrpRequirement(
        run_id=int(run.run_id),
        item_id=int(welded_item.item_id),
        total_required_qty=10,
        net_required_qty=10,
        period_from=run.period_from,
        period_to=run.period_to,
        bom_level=1,
        freeze_version=1,
    )
    db_session.add(welded_requirement)
    db_session.flush()
    welded_reservation = models.ReservationEntry(
        ledger_generation_id=int(generation.id),
        item_id=int(welded_item.item_id),
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        run_id=int(run.run_id),
        freeze_version=1,
        requirement_id=int(welded_requirement.id),
        priority_period_from=run.period_from,
        priority_period_to=run.period_to,
        realization_mode="make",
        reserved_qty=10,
        covered_from_stock_at_freeze_qty=0,
        replenishment_required_qty=10,
        replenishment_received_qty=0,
        realized_qty=0,
        lifecycle_status="active",
    )
    db_session.add(welded_reservation)
    db_session.flush()
    welded_work = models.ReplenishmentWorkItem(
        ledger_generation_id=int(generation.id),
        reservation_id=int(welded_reservation.id),
        plan_id=int(painted_work.plan_id),
        run_id=int(run.run_id),
        requirement_id=int(welded_requirement.id),
        item_id=int(welded_item.item_id),
        replenishment_method="make",
        replenishment_required_qty=10,
        replenishment_fulfilled_qty=0,
        replenishment_remaining_qty=10,
    )
    db_session.add(welded_work)
    db_session.flush()

    materials = material_availability.preview_make_work_item_materials(
        db_session,
        work_item_id=int(painted_work.id),
        item_id=int(painted_work.item_id),
        quantity=10,
        spec_id=None,
        ledger_generation_id=int(generation.id),
        order_number=f"MRP-R-{int(painted_work.requirement_id)}",
        run_id=int(run.run_id),
    )
    assert materials["coverage_basis"] == "welded_bom"
    assert materials["coverage_basis_item_id"] == welded_item.item_id
    assert [row["component_item_id"] for row in materials["components"]] == [raw_item.item_id]
    assert materials["components"][0]["required_qty"] == 20

    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    rows = {
        row.row_key: row.payload
        for row in db_session.query(models.PlanningReadRow).filter_by(snapshot_id=snapshot.id).all()
    }
    painted_row = rows[f"work-item:{painted_work.id}"]
    welded_row = rows[f"work-item:{welded_work.id}"]
    assert painted_row["coverage_status"] == "ready"
    assert painted_row["paint_weld_pair"]["role"] == "painted"
    assert painted_row["paint_weld_pair"]["counterpart_item_id"] == welded_item.item_id
    assert welded_row["paint_weld_pair"]["role"] == "welded"
    assert welded_row["available_actions"] == []
    assert "запуск выполняется из окрашенной строки" in welded_row["selection_disabled_reason"]

    # Promotion must accept the disabled welded proposal shape.  The promoter
    # previously required available_actions == ["materialize"] for every
    # proposal and rejected the welded row (available_actions == []) as a
    # "malformed proposal row", which blocked promotion of the whole
    # production-control journal candidate — and therefore every generation
    # acceptance that contained a weld→paint chain.
    _accept(db_session, generation, snapshot)
    promoted_welded = (
        db_session.query(models.PlanningReadRow)
        .filter_by(snapshot_id=snapshot.id, row_key=f"work-item:{welded_work.id}")
        .one()
    )
    assert promoted_welded.payload["available_actions"] == []
    assert snapshot.truth_status == "accepted"


def test_route_sheet_snapshot_builder_uses_candidate_generation_for_stock_bins(
    db_session,
    monkeypatch,
):
    accepted_generation = _building_generation(db_session, "route-bin-accepted")
    building_generation = _building_generation(db_session, "route-bin-building")

    accepted_generation.status = "accepted"
    accepted_generation.accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    accepted_generation.capabilities = dict(CAPABILITIES)
    publish_generation(db_session, accepted_generation)
    db_session.commit()

    main_item = models.Item(
        item_code="ROUTE-MAIN",
        item_name="Route Main",
        item_article="R-MAIN",
        unit="шт",
        status="active",
    )
    comp_item = models.Item(
        item_code="ROUTE-COMP",
        item_name="Route Component",
        item_article="R-COMP",
        unit="шт",
        status="active",
    )
    db_session.add_all([main_item, comp_item])
    db_session.flush()

    spec = models.Specification(
        spec_name="Route Snapshot Spec",
        spec_ref1c="ROUTE-SPEC",
    )
    db_session.add(spec)
    db_session.flush()

    db_session.add_all(
        [
            models.DefaultSpecification(
                item_id=main_item.item_id,
                spec_id=spec.spec_id,
            ),
            models.SpecComponent(
                spec_id=spec.spec_id,
                item_id=comp_item.item_id,
                quantity=1,
                component_type="Материал",
            ),
        ]
    )

    order = models.ProductionOrder(
        order_number="ROUTE-ORDER",
        order_date=datetime(2026, 7, 20),
        deletion_mark=False,
        source="1c",
    )
    db_session.add(order)
    db_session.flush()

    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=main_item.item_id,
        line_number=1,
        quantity=10,
        produced_qty=0,
        remaining_qty=10,
    )
    db_session.add(product)
    db_session.flush()

    db_session.add_all(
        [
            models.StockWarehouse(
                warehouse_ref1c="accepted-wh-a",
                warehouse_code="accepted-a",
                warehouse_name="Accepted A",
                is_selected=True,
            ),
            models.StockWarehouse(
                warehouse_ref1c="accepted-wh-b",
                warehouse_code="accepted-b",
                warehouse_name="Accepted B",
                is_selected=True,
            ),
            models.StockWarehouse(
                warehouse_ref1c="building-wh-a",
                warehouse_code="building-a",
                warehouse_name="Building A",
                is_selected=True,
            ),
        ]
    )
    db_session.flush()

    db_session.add_all(
        [
            models.StockBin(
                ledger_generation_id=accepted_generation.id,
                item_id=comp_item.item_id,
                characteristic_ref="",
                organization_ref="",
                warehouse_ref1c="accepted-wh-a",
                on_hand=5,
            ),
            models.StockBin(
                ledger_generation_id=accepted_generation.id,
                item_id=comp_item.item_id,
                characteristic_ref="",
                organization_ref="",
                warehouse_ref1c="accepted-wh-b",
                on_hand=7,
            ),
            models.StockBin(
                ledger_generation_id=building_generation.id,
                item_id=comp_item.item_id,
                characteristic_ref="",
                organization_ref="",
                warehouse_ref1c="building-wh-a",
                on_hand=9,
            ),
        ]
    )
    snapshot = build_candidate_snapshot(
        db_session,
        building_generation.id,
        accepted_run_ids=[],
    )

    route_payload = db_session.query(models.PlanningReadRow).filter_by(
        snapshot_id=snapshot.id,
        row_key=f"product:{product.product_id}",
    ).one().payload["_route_sheet_snapshot"]

    components = route_payload["sheet"]["components"]
    assert len(components) == 1
    assert components[0]["multi_stock_warning"] is False


def test_route_sheet_snapshot_rows_use_anchor_dedup_and_are_immutable(db_session):
    generation = _building_generation(db_session, "production-journal-route-chain")
    painted_product, welded_product = _setup_chain_journal_rows(db_session)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)

    first = read_route_sheet_snapshot_rows(
        db_session,
        [painted_product.product_id, welded_product.product_id],
    )
    assert len(first) == 1
    first_sheet = first[0]["sheet"]
    assert first[0]["anchor_product_id"] == painted_product.product_id
    assert int(first_sheet["chain"]["weld_product_id"]) == welded_product.product_id
    original_qty = first_sheet["remaining_qty"]

    first[0]["sheet"]["remaining_qty"] = 9999
    second = read_route_sheet_snapshot_rows(
        db_session,
        [painted_product.product_id, welded_product.product_id],
    )
    assert second[0]["sheet"]["remaining_qty"] == original_qty


def test_validate_candidate_snapshot_rejects_missing_route_sheet_payload(db_session):
    generation = _building_generation(db_session, "production-journal-route-invalid")
    _, _, product = _journal_line(db_session)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    row = db_session.query(models.PlanningReadRow).filter_by(
        snapshot_id=snapshot.id,
        row_key=f"product:{product.product_id}",
    ).one()
    payload = dict(row.payload)
    payload.pop("_route_sheet_snapshot", None)
    row.payload = payload
    db_session.flush()

    with pytest.raises(ProductionControlJournalPromotionError):
        validate_candidate_snapshot(
            db_session,
            snapshot,
            generation,
        )


def test_route_sheet_snapshot_rows_fail_closed_without_snapshot(db_session):
    generation = _building_generation(db_session, "production-journal-route-missing")
    generation.status = "accepted"
    generation.accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    generation.capabilities = dict(CAPABILITIES)
    publish_generation(db_session, generation)
    db_session.commit()

    with pytest.raises(RouteSheetSnapshotUnavailable) as caught:
        read_route_sheet_snapshot_rows(db_session, [1, 2, 3])
    assert caught.value.as_dict()["code"] == "route_sheet_snapshot_unavailable"


def test_public_read_is_persisted_paged_and_stable_after_live_mutation(db_session):
    generation = _building_generation(db_session, "production-journal-snapshot")
    item, order, product = _journal_line(db_session)

    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    assert build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    ) is snapshot
    assert snapshot.truth_status == "building"
    assert snapshot.payload["meta"]["row_count"] == 1
    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].payload["remaining_qty"] == 7
    _accept(db_session, generation, snapshot)

    first = read_snapshot(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert first["total"] == 1
    assert first["rows"][0]["product_id"] == product.product_id
    assert first["rows"][0]["remaining_qty"] == 7
    material_first = get_materials_snapshot(db_session, product.product_id)
    assert material_first["truth_status"] == "accepted"
    assert material_first["cutoff"] == snapshot.cutoff.isoformat()
    assert material_first["ledger_generation_id"] == generation.id
    assert material_first["product_id"] == product.product_id

    # Public reads are byte-stable for the generation and do not rebuild from
    # live rows, even when an operational writer changes those rows later.
    item.item_name = "MUTATED LIVE NAME"
    order.deletion_mark = True
    product.produced_qty = 10
    product.remaining_qty = 0
    db_session.commit()

    second = read_snapshot(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert second == first
    assert get_materials_snapshot(db_session, product.product_id) == material_first


def test_operator_quantity_is_live_while_accepted_output_stays_frozen(db_session):
    """Количество заказа — команда оператора, принятый выпуск — истина Ledger.

    Канон относит состояние исполнительных заказов к подвижному: потребность мы
    фиксируем, а заказ — уже исполнение. Поэтому изменённое оператором
    количество видно сразу, а `produced_qty` продолжает читаться из снимка.
    """
    generation = _building_generation(db_session, "production-journal-live-qty")
    item, order, product = _journal_line(db_session)
    snapshot = build_candidate_snapshot(
        db_session, generation.id, accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)

    before = read_snapshot(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert before["rows"][0]["quantity"] == 10
    assert before["rows"][0]["produced_qty"] == 3
    assert before["rows"][0]["remaining_qty"] == 7

    # Оператор уменьшил количество к запуску у ещё не выгруженного заказа.
    product.quantity = 4
    # Ledger своим чередом принял ещё выпуск — эта величина снимочная и в
    # журнал отсюда попасть не должна.
    product.produced_qty = 9
    db_session.commit()

    after = read_snapshot(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert after["rows"][0]["quantity"] == 4, "команда оператора обязана быть видна сразу"
    assert after["rows"][0]["produced_qty"] == 3, "принятый выпуск остаётся снимочным"
    assert after["rows"][0]["remaining_qty"] == 1, "остаток считается от снимочного выпуска"


def test_completed_1c_order_is_hidden_immediately_from_accepted_snapshot(db_session):
    generation = _building_generation(db_session, "production-journal-live-completion")
    _item, order, _product = _journal_line(db_session)
    snapshot = build_candidate_snapshot(
        db_session, generation.id, accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)

    before = read_snapshot(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert before["total"] == 1

    # The order was completed in 1C and its state was read back. Completion is
    # mutable execution state, so the immutable planning row is hidden rather
    # than rewritten while waiting for the next accepted generation.
    order.order_state_key = DONE_STATE_KEY
    db_session.commit()

    after = read_snapshot(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert after["rows"] == []
    assert after["total"] == 0
    assert after["offset"] == 0


def test_missing_snapshot_fails_closed_and_router_maps_it_to_503(db_session):
    generation = _building_generation(db_session, "production-journal-missing")
    generation.status = "accepted"
    generation.accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    generation.capabilities = dict(CAPABILITIES)
    publish_generation(db_session, generation)
    db_session.commit()

    with pytest.raises(ProductionControlJournalSnapshotUnavailable) as caught:
        read_snapshot(db_session)
    assert caught.value.as_dict()["status"] == "unavailable"
    assert "missing" in caught.value.as_dict()["reason"]

    with pytest.raises(HTTPException) as router_error:
        get_orders_journal(db=db_session)
    assert router_error.value.status_code == 503
    assert (
        router_error.value.detail["code"]
        == "production_control_journal_snapshot_unavailable"
    )


def test_stale_truth_fails_before_snapshot_lookup(db_session):
    generation = _building_generation(db_session, "production-journal-stale")
    _journal_line(db_session)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)
    generation.status = "stale"
    generation.reason = "refresh overdue"
    db_session.commit()

    with pytest.raises(ProductionControlJournalSnapshotUnavailable) as caught:
        read_snapshot(db_session)
    detail = caught.value.as_dict()
    assert detail["truth_status"] == "stale"
    assert detail["status"] == "unavailable"


def test_list_root_product_options_reads_only_frozen_snapshot_labels(db_session):
    generation = _building_generation(db_session, "journal-root-options")
    _journal_line(db_session)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    root_a = models.Item(
        item_code="ROOT-A",
        item_name="Root A",
        item_article="RA-1",
        unit="шт",
        status="active",
    )
    root_b = models.Item(
        item_code="ROOT-B",
        item_name="Root B",
        item_article="RB-2",
        unit="шт",
        status="active",
    )
    db_session.add_all([root_a, root_b])
    db_session.flush()
    payload = dict(snapshot.payload)
    payload["meta"] = {
        **dict(payload["meta"]),
        "root_product_options": [
            {"item_id": root_a.item_id, "item_name": "Root A", "item_article": "RA-1", "item_code": "ROOT-A"},
            {"item_id": root_b.item_id, "item_name": "Root B", "item_article": "RB-2", "item_code": "ROOT-B"},
        ],
    }
    snapshot.payload = payload
    _accept(db_session, generation, snapshot)

    root_a.item_name = "Renamed after acceptance"
    root_b.item_article = "ZZ-LIVE"
    db_session.flush()

    options = list_root_product_options(db_session)
    assert [item["item_id"] for item in options] == [root_a.item_id, root_b.item_id]
    assert options[0]["item_name"] == "Root A"
    assert options[1]["item_name"] == "Root B"


def _launch_after_cutoff(db, generation, work, *, created_at, quantity=10):
    """Открыть исполнительный заказ уже ПОСЛЕ cutoff принятого поколения."""
    order = models.ProductionOrder(
        order_number="SNAP-LAUNCH-1",
        order_date=datetime(2026, 7, 30),
        source="mrp",
        source_run_id=int(work.run_id),
        order_ref1c="snap-launch-ref",
        deletion_mark=False,
        created_at=created_at,
    )
    db.add(order)
    db.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=int(work.item_id),
        line_number=1,
        quantity=quantity,
        produced_qty=0,
        remaining_qty=quantity,
        source_mrp_requirement_id=int(work.requirement_id),
        ledger_generation_id=int(generation.id),
    )
    db.add(product)
    db.flush()
    db.commit()
    return order, product


def test_journal_shows_order_opened_after_cutoff_without_new_generation(db_session):
    """Запуск после cutoff виден сразу: «Не создан» по существующему документу
    1С — это потеря заказа для оператора, а не корректная заморозка плана."""
    generation = _building_generation(db_session, "production-journal-live-launch")
    run, work = _make_proposal(db_session, generation)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)

    before = read_snapshot(db_session, limit=100)
    proposal = next(
        row for row in before["rows"] if row["journal_row_key"] == f"work-item:{work.id}"
    )
    assert proposal["status"] == "not_created"
    assert proposal["product_id"] is None

    order, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )

    after = read_snapshot(db_session, limit=100)
    row = next(
        item for item in after["rows"] if item["journal_row_key"] == f"work-item:{work.id}"
    )
    assert row["status"] == "created"
    assert row["product_id"] == product.product_id
    assert row["order_id"] == order.order_id
    assert row["order_number"] == "SNAP-LAUNCH-1"
    assert row["order_ref1c"] == "snap-launch-ref"
    assert row["available_actions"] == ["close_1c"]
    # Плановые величины остаются снимочными: наложение касается только
    # исполнительной части строки.
    assert row["remaining_qty"] == 10
    assert row["coverage_status"] == "shortage"


def test_journal_overlays_print_and_transfer_state_after_cutoff(db_session):
    generation = _building_generation(db_session, "production-journal-live-state")
    run, work = _make_proposal(db_session, generation)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _order, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )
    printed_at = generation.cutoff.replace(tzinfo=None) + timedelta(minutes=45)
    db_session.add(
        models.ProductionOrderLineState(
            product_id=product.product_id,
            status="to_move",
            issue_status="exported",
            route_sheet_printed_at=printed_at,
        )
    )
    db_session.commit()

    row = next(
        item
        for item in read_snapshot(db_session, limit=100)["rows"]
        if item["journal_row_key"] == f"work-item:{work.id}"
    )

    assert row["status"] == "to_move"
    assert row["issue_status"] == "exported"
    assert row["route_sheet_printed_at"] == printed_at.isoformat()


def test_materials_are_available_for_order_opened_after_cutoff(db_session):
    generation = _building_generation(db_session, "production-journal-live-materials")
    run, work = _make_proposal(db_session, generation)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _order, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )

    materials = get_materials_snapshot(db_session, product.product_id)

    assert materials["product_id"] == product.product_id
    assert materials["ledger_generation_id"] == generation.id
    assert materials["truth_status"] == "accepted"
    assert materials["cutoff"] == snapshot.cutoff.isoformat()
    assert len(materials["components"]) == 1


def test_materials_endpoint_answers_through_its_strict_response_model(db_session):
    """Комплектация обязана проходить контракт ответа, а не только сборщик.

    `ProductionMaterialsResponse` запрещает лишние поля. Служебные ключи снимка
    наружу не выходят, и проверять это надо через саму ручку: тест, зовущий
    сборщик напрямую, проходит мимо модели ответа и такой отказ не ловит.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.routers.production_control import router as production_router

    generation = _building_generation(db_session, "production-journal-materials-api")
    _item, _order, product = _journal_line(db_session)
    snapshot = build_candidate_snapshot(
        db_session, generation.id, accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)

    app = FastAPI()
    app.include_router(production_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/production-control/orders/{product.product_id}/materials"
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["product_id"] == product.product_id
    assert "line_quantity" not in payload


def test_work_item_materials_answer_through_their_strict_response_model(db_session):
    """Та же проверка для строки-предложения: контракт ответа у ручек общий."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.routers.production_control import router as production_router

    generation = _building_generation(db_session, "production-journal-wi-materials-api")
    run, work = _make_proposal(db_session, generation)
    snapshot = build_candidate_snapshot(
        db_session, generation.id, accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)

    app = FastAPI()
    app.include_router(production_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/production-control/work-items/{work.id}/materials"
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["work_item_id"] == work.id
    assert "line_quantity" not in payload


def test_work_item_materials_remain_readable_from_the_published_row_generation(db_session):
    """Смена истины между списком и карточкой не обнуляет комплектующие."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.routers.production_control import router as production_router

    row_generation = _building_generation(db_session, "production-journal-wi-pinned")
    run, work = _make_proposal(db_session, row_generation)
    row_snapshot = build_candidate_snapshot(
        db_session, row_generation.id, accepted_run_ids=[run.run_id],
    )
    _accept(db_session, row_generation, row_snapshot)

    current_generation = _building_generation(db_session, "production-journal-wi-current")
    current_snapshot = build_candidate_snapshot(
        db_session, current_generation.id, accepted_run_ids=[],
    )
    _accept(db_session, current_generation, current_snapshot)

    app = FastAPI()
    app.include_router(production_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        stale_unpinned = client.get(
            f"/api/v1/production-control/work-items/{work.id}/materials"
        )
        pinned = client.get(
            f"/api/v1/production-control/work-items/{work.id}/materials",
            params={"ledger_generation_id": row_generation.id},
        )
        wrong_generation = client.get(
            f"/api/v1/production-control/work-items/{work.id}/materials",
            params={"ledger_generation_id": current_generation.id},
        )
    app.dependency_overrides.clear()

    assert stale_unpinned.status_code == 404
    assert wrong_generation.status_code == 404
    assert pinned.status_code == 200, pinned.text
    payload = pinned.json()
    assert payload["ledger_generation_id"] == row_generation.id
    assert payload["cutoff"] == row_snapshot.cutoff.isoformat()
    assert len(payload["components"]) == 1


def test_materials_survive_a_generation_flip_right_after_launch(db_session):
    """Заказ, выписанный за минуты до смены поколения, не должен пропадать.

    В снимок он не попал — создан после cutoff. Клеймо поколения на строке при
    этом осталось прежним: это отметка о том, при каком поколении заказ выписан,
    а не разрешение считать по нему покрытие. Раньше живой путь сверял клеймо с
    принятым поколением, заказ проваливался между двумя путями, и оператор
    получал material_coverage_snapshot_unavailable вместо комплектующих.
    """
    generation = _building_generation(db_session, "production-journal-gen-flip")
    run, work = _make_proposal(db_session, generation)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _order, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=13),
    )
    # Поколение сменилось уже после того, как заказ был выписан.
    product.ledger_generation_id = int(generation.id) - 1
    db_session.commit()

    materials = get_materials_snapshot(db_session, product.product_id)

    assert materials["product_id"] == product.product_id
    assert materials["ledger_generation_id"] == generation.id
    assert len(materials["components"]) == 1


def test_route_sheet_prints_for_order_opened_after_cutoff(db_session):
    """Маршрутный лист — документ по физическому заказу, а не плановая
    гипотеза: он обязан печататься сразу после запуска."""
    generation = _building_generation(db_session, "production-journal-live-route")
    run, work = _make_proposal(db_session, generation)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)

    _, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )

    rows = read_route_sheet_snapshot_rows(db_session, [product.product_id])
    assert len(rows) == 1
    assert int(rows[0]["anchor_product_id"]) == product.product_id


def test_route_sheet_still_fails_closed_for_unknown_product(db_session):
    """Наложение не превращается в дыру: изделия, которого нет ни в снимке, ни
    среди созданных после cutoff, по-прежнему нет."""
    generation = _building_generation(db_session, "production-journal-live-unknown")
    run, work = _make_proposal(db_session, generation)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)

    with pytest.raises(RouteSheetSnapshotUnavailable) as caught:
        read_route_sheet_snapshot_rows(db_session, [987654])
    assert "987654" in caught.value.as_dict()["reason"]


def test_order_deleted_in_1c_does_not_resurrect_journal_row(db_session):
    """Снятый пометкой удаления заказ не должен подменять строку-предложение."""
    generation = _building_generation(db_session, "production-journal-live-deleted")
    run, work = _make_proposal(db_session, generation)
    snapshot = build_candidate_snapshot(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)

    order, _product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )
    order.deletion_mark = True
    db_session.commit()

    rows = read_snapshot(db_session, limit=100)["rows"]
    row = next(item for item in rows if item["journal_row_key"] == f"work-item:{work.id}")
    assert row["status"] == "not_created"
    assert row["product_id"] is None
