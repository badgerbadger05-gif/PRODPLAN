import asyncio
from datetime import datetime, timezone

import pytest

from app import models
from app.routers.production_control import (
    CloseProductionOrderPayload,
    ProduceLinePayload,
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
