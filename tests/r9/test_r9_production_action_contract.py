import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import models
from app.routers.production_control import (
    CloseProductionOrderPayload,
    ExportProductionOrdersPayload,
    LineStatePayload,
    MaterialIssueCreatePayload,
    OrderLineQuantityPayload,
    ProduceLinePayload,
    post_export_production_orders_to_1c,
    post_material_issues,
    patch_order_line_quantity,
    patch_order_line_state,
    post_close_production_order,
    post_produce_line,
)


def _accepted_generation(db):
    cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key="r9-production-action-batch",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r9-production-action-generation",
        status="accepted",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        algorithm_version="r9-test",
        replay_version="r9-test",
        accepted_at=cutoff,
        physical_import_batch=batch,
    )
    db.add(generation)
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db.flush()
    return generation


def _current_production_scope(db, generation, *, revision="accepted:g1", identity="order:77"):
    db.add(models.CurrentExecutionScope(
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
        source_revision=revision,
        source_generation_id=generation.id,
        result_ready=True,
        content_hash="a" * 64,
        summary={},
    ))
    db.add(models.CurrentExecutionRow(
        entity_kind="production_control_journal",
        business_identity=identity,
        scope_key="production:all-live-orders",
        source_revision=revision,
        source_generation_id=generation.id,
        result_status="accepted",
        result_ready=True,
        content_hash="b" * 64,
        payload={"product_id": 77, "current_identity": identity},
    ))
    db.flush()


def test_produce_requires_current_identity_and_revision_before_service(monkeypatch, db_session):
    called = []
    import app.routers.production_control as router
    monkeypatch.setattr(router, "produce_line", lambda *args, **kwargs: called.append(True))

    with pytest.raises(Exception) as caught:
        post_produce_line(
            77,
            ProduceLinePayload(
                qty=1,
                current_identity="order:77",
                expected_source_revision="accepted:g1",
            ),
            db=db_session,
        )
    assert getattr(caught.value, "status_code", None) == 503
    assert called == []


def test_close_requires_current_identity_and_revision_before_order_lookup(monkeypatch, db_session):
    called = []
    import app.routers.production_control as router
    monkeypatch.setattr(router, "close_production_orders_to_1c", lambda *args, **kwargs: called.append(True))

    with pytest.raises(Exception) as caught:
        post_close_production_order(
            77,
            CloseProductionOrderPayload(
                current_identity="order:77",
                expected_source_revision="accepted:g1",
            ),
            db=db_session,
        )
    assert getattr(caught.value, "status_code", None) == 503
    assert called == []


def test_produce_rejects_stale_manifest_revision_before_service(monkeypatch, db_session):
    generation = _accepted_generation(db_session)
    _current_production_scope(db_session, generation, revision="accepted:g2")
    db_session.commit()
    called = []
    import app.routers.production_control as router
    monkeypatch.setattr(router, "produce_line", lambda *args, **kwargs: called.append(True))

    with pytest.raises(Exception) as caught:
        post_produce_line(
            77,
            ProduceLinePayload(
                qty=1,
                current_identity="order:77",
                expected_source_revision="accepted:g1",
            ),
            db=db_session,
        )
    assert getattr(caught.value, "status_code", None) == 409
    assert called == []


def test_produce_uses_current_identity_before_fake_1c_steps(monkeypatch, db_session):
    generation = _accepted_generation(db_session)
    _current_production_scope(db_session, generation)
    db_session.commit()
    import app.routers.production_control as router
    calls = []
    monkeypatch.setattr(router, "produce_line", lambda *args, **kwargs: {"manufacture_id": 8, "order_id": 9})
    monkeypatch.setattr(router, "export_manufactures_to_1c", lambda *args, **kwargs: calls.append("manufacture") or {"manufactures_error": 0, "entries": [{"target_ref_key": "m-1"}]})
    monkeypatch.setattr(router, "export_piecework_to_1c", lambda *args, **kwargs: calls.append("piecework") or {"manufactures_error": 0, "entries": [{"target_ref_key": "p-1"}]})
    import app.services.one_c_production_order_export as production_export
    monkeypatch.setattr(production_export, "finalize_produced_orders_to_1c", lambda *args, **kwargs: {"message": "ok", "resume_required": False})

    result = post_produce_line(
        77,
        ProduceLinePayload(
            qty=1,
            current_identity="order:77",
            expected_source_revision="accepted:g1",
        ),
        db=db_session,
    )
    assert result["ledger_readback"] == "queued"
    assert calls == ["manufacture", "piecework"]


def test_export_piecework_openapi_keeps_all_request_fields():
    from app.main import app
    schema = TestClient(app).get("/openapi.json").json()
    fields = schema["components"]["schemas"]["ExportPieceworkPayload"]["properties"]
    assert {"manufacture_ids", "operation_ref", "time_norm", "price", "organization_ref", "structural_unit_ref", "business_operation_ref", "dry_run", "allow_production"} <= set(fields)


@pytest.mark.parametrize(
    ("handler", "payload"),
    [
        (
            patch_order_line_state,
            LineStatePayload(status="done", current_identity="order:77", expected_source_revision="accepted:g1"),
        ),
        (
            patch_order_line_quantity,
            OrderLineQuantityPayload(quantity=2, current_identity="order:77", expected_source_revision="accepted:g1"),
        ),
    ],
)
def test_row_mutations_fail_closed_without_current_scope(monkeypatch, db_session, handler, payload):
    called = []
    import app.routers.production_control as router
    monkeypatch.setattr(router, "update_line_state", lambda *args, **kwargs: called.append("state"))
    monkeypatch.setattr(router, "update_local_order_quantity", lambda *args, **kwargs: called.append("quantity"))

    with pytest.raises(Exception) as caught:
        handler(77, payload, db=db_session)

    assert getattr(caught.value, "status_code", None) == 503
    assert called == []


def test_material_issue_requires_current_identity_set_before_service(monkeypatch, db_session):
    called = []
    import app.routers.production_control as router
    monkeypatch.setattr(router, "create_material_issues", lambda *args, **kwargs: called.append(True))

    with pytest.raises(Exception) as caught:
        post_material_issues(
            MaterialIssueCreatePayload(
                product_ids=[77],
                current_identities=[],
                expected_source_revision="accepted:g1",
            ),
            db=db_session,
        )

    assert getattr(caught.value, "status_code", None) == 409
    assert called == []


def test_production_export_rejects_unknown_current_identity_before_service(monkeypatch, db_session):
    generation = _accepted_generation(db_session)
    _current_production_scope(db_session, generation)
    db_session.commit()
    called = []
    import app.routers.production_control as router
    monkeypatch.setattr(router, "export_production_orders_to_1c", lambda *args, **kwargs: called.append(True))

    with pytest.raises(Exception) as caught:
        post_export_production_orders_to_1c(
            ExportProductionOrdersPayload(
                order_ids=[77],
                current_identities=["missing"],
                expected_source_revision="accepted:g1",
            ),
            db=db_session,
        )

    assert getattr(caught.value, "status_code", None) == 503
    assert called == []
