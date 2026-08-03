from datetime import datetime, timezone

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
