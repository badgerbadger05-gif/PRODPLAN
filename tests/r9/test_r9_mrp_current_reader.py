from datetime import datetime, timezone, date

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_rows,
    publish_current_obligation_views_from_generation,
    require_current_execution_scope,
)
from app.services.mrp_result_snapshot import (
    read_mrp_result_manifest,
    read_mrp_result_rows,
)
from app.routers.plan import (
    _mrp_snapshot_identity,
    export_planning_result_production,
    export_planning_result_purchases,
    export_planning_result_purchases_to_1c,
    export_planning_result_rework,
    get_planning_result_production,
    get_planning_result_production_grouped,
    get_planning_result_purchases,
    get_planning_result_purchases_grouped,
    get_planning_result_purchases_grouped_by_category,
    get_planning_result_rework,
    get_planning_result_rework_grouped,
    get_planning_result_rework_grouped_by_category,
    get_planning_result_capacity,
    get_planning_result_summary,
    PurchaseOrder1CExportRequest,
)


def _generation(db):
    cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key="r9-mrp-reader-batch",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r9-mrp-reader-generation",
        status="accepted",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True, "reservation_replay": True},
        physical_import_batch=batch,
        algorithm_version="r9-test",
        replay_version="r9-test",
        accepted_at=cutoff,
    )
    db.add(generation)
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db.flush()
    return generation


def _mrp_snapshot(db, generation):
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key="run:41:v1",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={"summary": {"planned": 7}},
        published_at=generation.cutoff,
    )
    db.add(snapshot)
    db.flush()
    db.add(models.PlanningReadRow(
        snapshot_id=snapshot.id,
        row_key="req:41:1",
        row_kind="production",
        item_id=10,
        sort_key="2026-09-10|0001",
        payload={"item_id": 10, "qty": 7, "run_id": 41, "row_kind": "production"},
    ))
    db.flush()


def test_mrp_reader_uses_persisted_current_rows_and_manifest(db_session):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    result = read_mrp_result_rows(db_session, 41, row_kind="production")
    manifest = read_mrp_result_manifest(db_session, 41)

    assert result["rows"][0].items() >= {
        "item_id": 10,
        "qty": 7,
        "run_id": 41,
        "row_kind": "production",
    }.items()
    assert result["current_identity"]
    assert result["source_revision"].startswith("accepted:g")
    assert manifest["current_identity"] == result["current_identity"]


def test_mrp_current_identity_filters_before_pagination(db_session):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    snapshot = db_session.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == "mrp_result",
    ).one()
    db_session.add(models.PlanningReadRow(
        snapshot_id=snapshot.id,
        row_key="req:41:2",
        row_kind="production",
        item_id=11,
        sort_key="2026-09-10|0002",
        payload={"item_id": 11, "qty": 8, "run_id": 41, "row_kind": "production"},
    ))
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()
    current = load_current_execution_rows(
        db_session, entity_kind="mrp_result", scope_key="mrp:all-live-plans",
    )
    target = next(row.business_identity for row in current if row.payload.get("item_id") == 11)

    result = read_mrp_result_rows(
        db_session, 41, row_kind="production", current_identity=target,
        limit=1, offset=0,
    )

    assert result["total"] == 1
    assert result["rows"][0]["current_identity"] == target
    assert result["rows"][0]["item_id"] == 11

    missing = read_mrp_result_rows(
        db_session, 41, row_kind="production", current_identity="mrp:missing",
        limit=1, offset=0,
    )
    assert missing["total"] == 0
    assert missing["rows"] == []


def test_mrp_reader_fails_closed_even_when_legacy_snapshot_exists(db_session):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()

    with pytest.raises(CurrentExecutionUnavailable):
        read_mrp_result_rows(db_session, 41, row_kind="production")


def test_mrp_http_reader_maps_missing_current_to_503(db_session):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()

    with pytest.raises(Exception) as caught:
        import asyncio
        asyncio.run(get_planning_result_summary(41, db=db_session))
    assert getattr(caught.value, "status_code", None) == 503


@pytest.mark.parametrize(
    "endpoint,kwargs",
    [
        (get_planning_result_production, {"item_id": None, "root_item_id": None, "bucket_type": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None, "snapshot_id": None}),
        (get_planning_result_production_grouped, {"item_id": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None}),
        (get_planning_result_purchases, {"item_id": None, "root_item_id": None, "bucket_type": None, "supplier_ref1c": None, "category_id": None, "category_ref1c": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None, "snapshot_id": None}),
        (get_planning_result_purchases_grouped, {"date_from": None, "date_to": None, "limit": 100, "offset": 0}),
        (get_planning_result_rework, {"item_id": None, "root_item_id": None, "bucket_type": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None, "snapshot_id": None}),
        (get_planning_result_rework_grouped, {"item_id": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None}),
        (get_planning_result_purchases_grouped_by_category, {"item_id": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None}),
        (get_planning_result_rework_grouped_by_category, {"item_id": None, "date_from": None, "date_to": None, "limit": 100, "offset": 0, "sort_by": None, "sort_dir": None}),
        (get_planning_result_capacity, {"area_id": None, "bucket_type": None, "date_from": None, "date_to": None, "limit": 200, "offset": 0, "snapshot_id": None}),
        (export_planning_result_production, {"format": "csv", "root_item_id": None, "bucket_type": None, "date_from": None, "date_to": None, "sort_by": None, "sort_dir": None, "snapshot_id": None}),
        (export_planning_result_purchases, {"format": "csv", "root_item_id": None, "bucket_type": None, "supplier_ref1c": None, "category_id": None, "category_ref1c": None, "date_from": None, "date_to": None, "sort_by": None, "sort_dir": None, "snapshot_id": None}),
        (export_planning_result_rework, {"format": "csv", "root_item_id": None, "bucket_type": None, "date_from": None, "date_to": None, "sort_by": None, "sort_dir": None, "snapshot_id": None}),
    ],
)
def test_mrp_detail_grouped_and_export_never_fall_back_to_legacy_snapshot(
    db_session, endpoint, kwargs
):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()

    call = {"run_id": 41, "db": db_session, **kwargs}
    with pytest.raises(Exception) as caught:
        import asyncio
        asyncio.run(endpoint(**call))
    assert getattr(caught.value, "status_code", None) == 503


def test_mrp_purchases_to_1c_requires_current_identity_and_never_calls_external_exporter(
    db_session, monkeypatch
):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()
    called = []

    def fake_exporter(**kwargs):
        called.append(kwargs)
        return {"status": "ok"}

    import app.routers.plan as plan_router
    monkeypatch.setattr(plan_router, "export_planned_purchases_to_1c", fake_exporter)

    import asyncio
    with pytest.raises(Exception) as caught:
        asyncio.run(
            export_planning_result_purchases_to_1c(
                41,
                PurchaseOrder1CExportRequest(
                    current_identities=["mrp-run:41"],
                    expected_source_revision="accepted:g1",
                    purchase_ids=[1],
                ),
                db=db_session,
            )
        )
    assert getattr(caught.value, "status_code", None) == 503
    assert called == []


def test_mrp_reader_rejects_unknown_run_and_keeps_date_to_inclusive(db_session):
    generation = _generation(db_session)
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key="run:51:v1",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={"summary": {"row_counts": {"production": 1}, "total_qty": {"production": 3}}},
        published_at=generation.cutoff,
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.add(models.PlanningReadRow(
        snapshot_id=snapshot.id,
        row_key="req:51:1",
        row_kind="production",
        item_id=10,
        sort_key="2026-09-10|0001",
        payload={"item_id": 10, "qty": 3, "run_id": 51, "row_kind": "production"},
    ))
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    assert read_mrp_result_rows(
        db_session, 51, row_kind="production", date_to="2026-09-10"
    )["total"] == 1
    with pytest.raises(CurrentExecutionUnavailable):
        read_mrp_result_manifest(db_session, 999)


def test_mrp_reader_uses_per_run_summary_and_keeps_identity_tie_ascending(db_session):
    generation = _generation(db_session)
    for run_id, total in ((61, 2), (62, 9)):
        snapshot = models.PlanningReadSnapshot(
            consumer="mrp_result",
            snapshot_key=f"run:{run_id}:v1",
            ledger_generation_id=generation.id,
            cutoff=generation.cutoff,
            truth_status="accepted",
            payload={"summary": {"row_counts": {"production": 1}, "total_qty": {"production": total}}},
            published_at=generation.cutoff,
        )
        db_session.add(snapshot)
        db_session.flush()
        for key in ("b", "a"):
            db_session.add(models.PlanningReadRow(
                snapshot_id=snapshot.id,
                row_key=key,
                row_kind="production",
                item_id=10,
                sort_key="2026-09-10|same",
                payload={"item_id": 10, "qty": 1, "run_id": run_id, "row_kind": "production", "agg_key": key},
            ))
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    manifest = read_mrp_result_manifest(db_session, 62)
    rows = read_mrp_result_rows(db_session, 62, row_kind="production", sort_dir="desc")
    assert manifest["snapshot_total_qty"] == {"production": 9}
    assert [row["current_identity"] for row in rows["rows"]] == [
        "mrp-run:62:production:a", "mrp-run:62:production:b"
    ]


def test_mrp_grouped_identity_uses_current_business_anchor(db_session):
    generation = _generation(db_session)
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key="run:63:v1",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={"summary": {"row_counts": {"production": 0}, "total_qty": {"production": 0}}},
        published_at=generation.cutoff,
    )
    db_session.add(snapshot)
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    scope = require_current_execution_scope(
        db_session,
        entity_kind="mrp_result",
        scope_key="mrp:all-live-plans",
    )
    identity = _mrp_snapshot_identity(db_session, 63, scope.id)

    assert identity["current_identity"] == "mrp-run:63"
    assert identity["source_revision"].startswith("accepted:g")


def test_mrp_export_returns_current_identity_and_revision(db_session):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()
    scope = require_current_execution_scope(
        db_session,
        entity_kind="mrp_result",
        scope_key="mrp:all-live-plans",
    )

    import asyncio
    result = asyncio.run(
        export_planning_result_production(
            41,
            format="csv",
            snapshot_id=scope.id,
            db=db_session,
        )
    )

    assert result["current_identity"] == "mrp-run:41"
    assert result["source_revision"] == scope.source_revision


def test_mrp_rework_export_returns_current_identity_and_revision(db_session):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()
    scope = require_current_execution_scope(
        db_session,
        entity_kind="mrp_result",
        scope_key="mrp:all-live-plans",
    )

    import asyncio
    result = asyncio.run(
        export_planning_result_rework(
            41,
            format="csv",
            snapshot_id=scope.id,
            db=db_session,
        )
    )

    assert result["current_identity"] == "mrp-run:41"
    assert result["source_revision"] == scope.source_revision


def test_mrp_purchases_to_1c_resolves_current_identity_and_revision(
    db_session, monkeypatch
):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    snapshot = db_session.query(models.PlanningReadSnapshot).filter_by(
        snapshot_key="run:41:v1"
    ).one()
    db_session.add(models.PlanningReadRow(
        snapshot_id=snapshot.id,
        row_key="purchase:41:1",
        row_kind="purchase",
        item_id=10,
        sort_key="2026-09-10|0002",
        payload={
            "item_id": 10,
            "purchase_id": 7001,
            "qty": 2,
            "run_id": 41,
            "row_kind": "purchase",
            "unit": "шт",
            "bucket_date": "2026-09-10",
        },
    ))
    db_session.add(models.PlannedPurchase(
        purchase_id=7001,
        run_id=41,
        item_id=10,
        requested_qty=2,
        planned_qty=2,
        qty=2,
        need_date=date(2026, 9, 10),
        order_date=date(2026, 9, 10),
        lead_time_days=1,
        bucket_date=date(2026, 9, 10),
    ))
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()
    scope = require_current_execution_scope(
        db_session,
        entity_kind="mrp_result",
        scope_key="mrp:all-live-plans",
    )
    current_identity = "mrp-run:41:purchase:item:10|unit:шт"
    fake_state = {"calls": 0, "sends": 0, "persisted_ref": None}

    def fake_exporter(**kwargs):
        fake_state["calls"] += 1
        # Local fake 1C: the first request persists a document and times out;
        # retry performs read-back and must not issue a second create/send.
        if fake_state["persisted_ref"] is None:
            fake_state["sends"] += 1
            fake_state["persisted_ref"] = "fake-1c-doc-1"
            return {"status": "partial_error", "orders_created": 0, "target_ref": None}
        return {"status": "ok", "orders_created": 0, "orders_existing": 1, "target_ref": fake_state["persisted_ref"]}

    import app.routers.plan as plan_router
    monkeypatch.setattr(plan_router, "export_planned_purchases_to_1c", fake_exporter)
    import asyncio
    result = asyncio.run(
        export_planning_result_purchases_to_1c(
            41,
            PurchaseOrder1CExportRequest(
                current_identities=[current_identity],
                expected_source_revision=scope.source_revision,
                purchase_ids=[7001],
            ),
            db=db_session,
        )
    )

    assert result["current_identity"] == "mrp-run:41"
    assert result["source_revision"] == scope.source_revision
    assert result["current_identities"] == [current_identity]
    assert result["idempotency_key"].startswith("mrp-purchases:")
    assert fake_state["calls"] == 1

    retry = asyncio.run(
        export_planning_result_purchases_to_1c(
            41,
            PurchaseOrder1CExportRequest(
                current_identities=[current_identity],
                expected_source_revision=scope.source_revision,
                purchase_ids=[7001],
            ),
            db=db_session,
        )
    )
    assert retry["idempotency_key"] == result["idempotency_key"]
    assert fake_state["calls"] == 2
    assert fake_state["sends"] == 1


def test_mrp_purchases_to_1c_rejects_unknown_or_foreign_current_identity(
    db_session, monkeypatch
):
    generation = _generation(db_session)
    _mrp_snapshot(db_session, generation)
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()
    scope = require_current_execution_scope(
        db_session,
        entity_kind="mrp_result",
        scope_key="mrp:all-live-plans",
    )
    called = []
    import app.routers.plan as plan_router
    monkeypatch.setattr(
        plan_router,
        "export_planned_purchases_to_1c",
        lambda **kwargs: called.append(kwargs),
    )
    import asyncio
    with pytest.raises(Exception) as caught:
        asyncio.run(
            export_planning_result_purchases_to_1c(
                41,
                PurchaseOrder1CExportRequest(
                    current_identities=["mrp-run:999:purchase:1"],
                    expected_source_revision=scope.source_revision,
                ),
                db=db_session,
            )
        )
    assert getattr(caught.value, "status_code", None) == 409
    assert called == []


def test_mrp_root_filter_uses_persisted_current_membership(db_session):
    generation = _generation(db_session)
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key="run:71:v1",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={"summary": {"row_counts": {"production": 1}, "total_qty": {"production": 4}}},
        published_at=generation.cutoff,
    )
    db_session.add(snapshot)
    db_session.flush()
    row = models.PlanningReadRow(
        snapshot_id=snapshot.id,
        row_key="req:71:1",
        row_kind="production",
        item_id=10,
        sort_key="2026-09-10|0001",
        payload={"item_id": 10, "qty": 4, "run_id": 71, "row_kind": "production"},
    )
    db_session.add(row)
    db_session.flush()
    db_session.add(models.PlanningReadRootMember(
        snapshot_id=snapshot.id,
        row_id=row.id,
        root_key="root:99",
        root_item_id=99,
    ))
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    assert read_mrp_result_rows(
        db_session, 71, row_kind="production", root_item_id=99
    )["total"] == 1
    assert read_mrp_result_rows(
        db_session, 71, row_kind="production", root_item_id=100
    )["total"] == 0
