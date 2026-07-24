from datetime import date, datetime, timezone

import pytest

from app import models
from app.services.obligation_refresh_publish import (
    ObligationRefreshPublishError,
    publish_obligation_refresh_batch,
)
from app.services.obligation_refresh_manifest import (
    MANIFEST_HASH_KEY,
    MANIFEST_KEY,
    create_obligation_refresh_manifest,
)


def _generation(db, *, key, status, cutoff, watermarks=None):
    physical = models.PhysicalImportBatch(
        batch_key=f"physical:{key}", status="completed", cutoff=cutoff,
        source_watermarks={}, completed_at=cutoff,
    )
    row = models.LedgerGeneration(
        generation_key=key, status=status, cutoff=cutoff,
        source_watermarks=watermarks or {}, capabilities={},
        physical_import_batch=physical, algorithm_version="tests/1",
        accepted_at=cutoff if status == "accepted" else None,
    )
    db.add(row)
    db.flush()
    return row


def _capabilities():
    return {
        "physical_ledger": True,
        "reservation_replay": True,
        "execution_allocations": True,
        "planning_snapshots": True,
        "dbr_feeder_cockpit": False,
        "dbr_purchase_cockpit": False,
        "purchase_control_journal": True,
    }


def _seal_build(db, target, candidates, cutoff):
    target.capabilities = _capabilities()
    for stage in ("physical_import", "reservation_materialize", "reservation_replay", "snapshot_build"):
        metrics = {}
        if stage == "snapshot_build":
            metrics = {
                "candidate_run_ids": [row.run_id for row in candidates],
                "candidate_read_snapshot_ids": {
                    str(row.run_id): row._test_read_snapshot_id for row in candidates
                },
                "future_supply_captured": True,
                "dbr_cockpit_ready": False,
                "dbr_purchase_ready": False,
                "purchase_control_journal_snapshot_id": target._test_purchase_journal_snapshot_id,
            }
        db.add(models.LedgerBuildBatch(
            ledger_generation_id=target.id, stage=stage, batch_key=f"{target.id}:{stage}",
            status="completed", algorithm_version="tests/1", metrics=metrics,
            completed_at=cutoff,
        ))
    db.flush()


def _candidate_read_snapshots(db, target, candidates, cutoff):
    """Persist minimal MRP read snapshots; zero rows in every kind are valid."""
    for candidate in candidates:
        snapshot = models.PlanningReadSnapshot(
            consumer="mrp_result", snapshot_key=f"run:{candidate.run_id}",
            ledger_generation_id=target.id, cutoff=cutoff, truth_status="building",
            reason="unpublished candidate snapshot",
            payload={
                "run_id": candidate.run_id,
                "row_counts": {
                    "production": 0, "purchase": 0, "rework": 0, "capacity": 0,
                },
            },
            published_at=cutoff,
        )
        db.add(snapshot)
        db.flush()
        # Test-only transient marker avoids widening the production contract.
        candidate._test_read_snapshot_id = snapshot.id
    purchase_journal = models.PlanningReadSnapshot(
        consumer="purchase_control_journal",
        snapshot_key="journal:v1",
        ledger_generation_id=target.id,
        cutoff=cutoff,
        truth_status="building",
        reason="unpublished Ledger-native purchase journal",
        payload={
            "meta": {
                "ledger_generation": target.id,
                "ledger_generation_id": target.id,
                "cutoff": cutoff.isoformat(),
                "truth_status": "building",
                "fact_source": "ledger",
                "received_qty_status": "unavailable",
                "read_only": True,
            },
            "rows": [],
            "cards": {},
        },
        published_at=cutoff,
    )
    db.add(purchase_journal)
    db.flush()
    target._test_purchase_journal_snapshot_id = purchase_journal.id


def _set_purchase_journal_rows(db_session, target, *, rows):
    snapshot = db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=target.id, consumer="purchase_control_journal", snapshot_key="journal:v1"
    ).one_or_none()
    assert snapshot is not None
    payload = dict(snapshot.payload)
    payload["rows"] = rows
    snapshot.payload = payload
    db_session.flush()


def _batch(db, count=2, add_count=0):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    parent_generation = _generation(db, key="accepted", status="accepted", cutoff=cutoff)
    db.add(models.PlanningTruthState(id=1, current_generation_id=parent_generation.id))
    db.flush()
    parents = []
    for index in range(count):
        plan = models.ProductionPlanHeader(
            name=f"source {index}", period_from=date(2026, 7, 1), period_to=date(2026, 7, 31),
        )
        db.add(plan)
        db.flush()
        parent = models.PlanningRun(
            status="FIXED_SNAPSHOT", ledger_generation_id=parent_generation.id,
            source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
            horizon_days=90, config_snapshot={"source": index}, pinned=True,
            fixed_at=cutoff, finished_at=cutoff,
        )
        db.add(parent)
        db.flush()
        parents.append(parent)
    target = _generation(
        db, key="refresh", status="building", cutoff=cutoff,
        watermarks={"generation_kind": "obligation_refresh", "parent_generation_id": parent_generation.id},
    )
    # An obligation refresh reuses the accepted immutable physical prefix.
    target.physical_import_batch_id = parent_generation.physical_import_batch_id
    db.flush()
    additions = []
    for index in range(add_count):
        plan = models.ProductionPlanHeader(
            name=f"added {index}", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
            status="fixed",
        )
        db.add(plan)
        db.flush()
        additions.append(plan)
    manifest = create_obligation_refresh_manifest(
        db, parent_generation.id, target.id, [row.id for row in additions],
        started_by="test", horizon_days=90, config_version_id=None,
        config_snapshot={"added": True},
    )
    candidates = [db_session_row for db_session_row in db.query(models.PlanningRun).filter(
        models.PlanningRun.run_id.in_([int(entry["candidate_run_id"]) for entry in manifest.entries])
    ).all()]
    _candidate_read_snapshots(db, target, candidates, cutoff)
    _seal_build(db, target, candidates, cutoff)
    db.commit()
    return cutoff, parent_generation, target, parents, candidates


def _publish(db, parent, target, cutoff, capabilities=None):
    return publish_obligation_refresh_batch(
        db, parent_generation_id=parent.id, target_generation_id=target.id,
        accepted_at=cutoff, capabilities=capabilities or _capabilities(),
    )


def _line_item(db, suffix):
    item = models.Item(item_code=f"publish-item-{suffix}", item_name="publish item")
    db.add(item)
    db.flush()
    return item


def test_publish_requires_candidate_for_every_active_source_plan(db_session):
    cutoff, parent, target, _parents, candidates = _batch(db_session)
    db_session.delete(candidates[-1])
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match="missing or extra candidates"):
        _publish(db_session, parent, target, cutoff)

    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id
    assert db_session.get(models.LedgerGeneration, target.id).status == "building"


def test_publish_is_atomic_under_caller_rollback(db_session):
    cutoff, parent, target, parents, candidates = _batch(db_session)

    result = _publish(db_session, parent, target, cutoff)
    assert result.published is True
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == target.id
    assert [row.status for row in parents] == ["SUPERSEDED", "SUPERSEDED"]
    assert [row.status for row in candidates] == ["FIXED_SNAPSHOT", "FIXED_SNAPSHOT"]
    snapshots = db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=target.id, consumer="mrp_result"
    ).all()
    assert [row.truth_status for row in snapshots] == ["accepted", "accepted"]
    assert all(
        row.reason is None
        and row.published_at.replace(tzinfo=timezone.utc) == cutoff
        for row in snapshots
    )

    db_session.rollback()
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id
    assert db_session.get(models.LedgerGeneration, target.id).status == "building"
    assert [db_session.get(models.PlanningRun, row.run_id).status for row in parents] == [
        "FIXED_SNAPSHOT", "FIXED_SNAPSHOT"
    ]
    assert [db_session.get(models.PlanningRun, row.run_id).status for row in candidates] == [
        "BUILDING_SNAPSHOT", "BUILDING_SNAPSHOT"
    ]
    snapshots = db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=target.id, consumer="mrp_result"
    ).all()
    assert [row.truth_status for row in snapshots] == ["building", "building"]
    assert all(row.reason == "unpublished candidate snapshot" for row in snapshots)


def test_publish_exact_retry_is_noop_but_mixed_state_is_rejected(db_session):
    cutoff, parent, target, _parents, _candidates = _batch(db_session)
    first = _publish(db_session, parent, target, cutoff)
    db_session.commit()

    retry = _publish(db_session, parent, target, cutoff)
    assert retry.published is False
    assert retry.parent_run_ids == first.parent_run_ids
    assert retry.candidate_run_ids == first.candidate_run_ids

    target.capabilities = {"ledger": "different"}
    db_session.flush()
    with pytest.raises(ObligationRefreshPublishError, match="mixed or partial"):
        _publish(db_session, parent, target, cutoff)


def test_publish_legacy_parent_without_direct_generation_id_is_supported_and_exact_retry_is_noop(db_session):
    cutoff, parent, target, parents, candidates = _batch(db_session, count=1)
    legacy_parent = parents[0]
    legacy_parent.ledger_generation_id = None
    item = models.Item(
        item_code="legacy-lineage-item", item_name="legacy lineage item",
        unit="шт", replenishment_method="Покупка", replenishment_time=7, stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    req = models.MrpRequirement(
        run_id=legacy_parent.run_id, item_id=item.item_id, total_required_qty=0,
        net_required_qty=0, period_from=legacy_parent.period_from,
        period_to=legacy_parent.period_to, bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=parent.id,
        item_id=item.item_id,
        run_id=legacy_parent.run_id,
        freeze_version=0,
        requirement_id=req.id,
        priority_period_from=legacy_parent.period_from,
        priority_period_to=legacy_parent.period_to,
    ))
    db_session.flush()

    first = _publish(db_session, parent, target, cutoff)
    assert first.published is True
    assert first.parent_run_ids == (legacy_parent.run_id,)
    assert first.candidate_run_ids == (candidates[0].run_id,)
    db_session.commit()

    retry = _publish(db_session, parent, target, cutoff)
    assert retry.published is False
    assert retry.parent_run_ids == first.parent_run_ids
    assert retry.candidate_run_ids == first.candidate_run_ids


def test_publish_rejects_candidate_with_external_export_link(db_session):
    cutoff, parent, target, _parents, candidates = _batch(db_session)
    order = models.ProductionOrder(
        order_number="candidate-export", order_date=cutoff,
        order_ref1c="candidate-export-ref", source="mrp", source_run_id=candidates[0].run_id,
    )
    db_session.add(order)
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match="external export"):
        _publish(db_session, parent, target, cutoff)


@pytest.mark.parametrize("mutation, error", [
    ("empty_capabilities", "pre-sealed"),
    ("incomplete_capabilities", "capabilities are incomplete"),
    ("partial_checkpoint", "reservation_replay"),
    ("missing_manifest", "candidate manifest"),
])
def test_publish_requires_sealed_complete_build(db_session, mutation, error):
    cutoff, parent, target, _parents, _candidates = _batch(db_session)
    if mutation == "empty_capabilities":
        target.capabilities = {}
    elif mutation == "incomplete_capabilities":
        target.capabilities = {
            **_capabilities(),
            "planning_snapshots": False,
        }
    elif mutation == "partial_checkpoint":
        row = db_session.query(models.LedgerBuildBatch).filter_by(
            ledger_generation_id=target.id, stage="reservation_replay"
        ).one()
        row.status = "building"
        row.completed_at = None
    else:
        row = db_session.query(models.LedgerBuildBatch).filter_by(
            ledger_generation_id=target.id, stage="snapshot_build"
        ).one()
        row.metrics = {"future_supply_captured": True}
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match=error):
        _publish(
            db_session,
            parent,
            target,
            cutoff,
            capabilities=(
                dict(target.capabilities)
                if mutation == "incomplete_capabilities"
                else None
            ),
        )


def test_exact_retry_allows_legitimate_export_after_publication(db_session):
    cutoff, parent, target, _parents, candidates = _batch(db_session)
    _publish(db_session, parent, target, cutoff)
    db_session.commit()
    db_session.add(models.ProductionOrder(
        order_number="post-publish", order_date=cutoff, order_ref1c="post-publish-ref",
        source="mrp", source_run_id=candidates[0].run_id,
    ))
    db_session.commit()

    assert _publish(db_session, parent, target, cutoff).published is False


def test_publish_allows_first_add_only_and_exact_retry(db_session):
    cutoff, parent, target, parents, candidates = _batch(db_session, count=0, add_count=1)

    result = _publish(db_session, parent, target, cutoff)
    assert result.published is True
    assert result.parent_run_ids == ()
    assert result.candidate_run_ids == (candidates[0].run_id,)
    assert candidates[0].status == "FIXED_SNAPSHOT"
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == target.id
    db_session.commit()

    # Publication was the only point that forbade external links.  A later
    # legitimate export must not make an exact retry look like a partial batch.
    db_session.add(models.ProductionOrder(
        order_number="post-publish-add", order_date=cutoff,
        order_ref1c="post-publish-add-ref", source="mrp",
        source_run_id=candidates[0].run_id,
    ))
    db_session.commit()
    assert _publish(db_session, parent, target, cutoff).published is False


def test_publish_allows_refresh_and_add_together(db_session):
    cutoff, parent, target, parents, candidates = _batch(db_session, count=1, add_count=1)

    result = _publish(db_session, parent, target, cutoff)
    assert result.published is True
    assert result.parent_run_ids == (parents[0].run_id,)
    assert set(result.candidate_run_ids) == {row.run_id for row in candidates}
    assert parents[0].status == "SUPERSEDED"
    assert all(row.status == "FIXED_SNAPSHOT" for row in candidates)
    assert db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=target.id, truth_status="accepted"
        ).count() == 3


@pytest.mark.parametrize("mutation, error", [
    ("missing", "foreign or extra"),
    ("extra", "foreign or extra"),
    ("wrong_count", "persisted rows conflict"),
    ("missing_kind", "row_counts are incomplete"),
])
def test_publish_rejects_incomplete_or_tampered_candidate_read_snapshots(
    db_session, mutation, error
):
    cutoff, parent, target, _parents, candidates = _batch(db_session, count=1)
    snapshot = db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=target.id, consumer="mrp_result"
    ).one()
    checkpoint = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=target.id, stage="snapshot_build"
    ).one()
    if mutation == "missing":
        db_session.delete(snapshot)
    elif mutation == "extra":
        db_session.add(models.PlanningReadSnapshot(
            consumer="mrp_result", snapshot_key="run:9999", ledger_generation_id=target.id,
            cutoff=cutoff, truth_status="building", reason="unpublished candidate snapshot",
            payload={"row_counts": {"production": 0, "purchase": 0, "rework": 0, "capacity": 0}},
            published_at=cutoff,
        ))
    elif mutation == "wrong_count":
        snapshot.payload = {**snapshot.payload, "row_counts": {
            "production": 1, "purchase": 0, "rework": 0, "capacity": 0,
        }}
    else:
        snapshot.payload = {**snapshot.payload, "row_counts": {"production": 0}}
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match=error):
        _publish(db_session, parent, target, cutoff)


def test_exact_retry_rejects_tampered_accepted_candidate_read_snapshot(db_session):
    cutoff, parent, target, _parents, _candidates = _batch(db_session, count=1)
    _publish(db_session, parent, target, cutoff)
    db_session.commit()
    snapshot = db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=target.id, consumer="mrp_result"
    ).one()
    snapshot.reason = "tampered"
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match="mixed or partial"):
        _publish(db_session, parent, target, cutoff)


def test_publish_transfers_refresh_locks_and_acquires_add_locks(db_session):
    cutoff, parent, target, parents, candidates = _batch(db_session, count=1, add_count=1)
    refresh_candidate = next(row for row in candidates if row.prior_run_id is not None)
    add_candidate = next(row for row in candidates if row.prior_run_id is None)
    item = _line_item(db_session, "locks")
    refresh_line = models.ProductionPlanLine(
        plan_id=parents[0].source_plan_id, item_id=item.item_id,
        bucket_date=date(2026, 7, 1), qty=1, locked_by_run_id=parents[0].run_id,
    )
    add_line = models.ProductionPlanLine(
        plan_id=add_candidate.source_plan_id, item_id=item.item_id,
        bucket_date=date(2026, 8, 1), qty=1,
    )
    db_session.add_all([refresh_line, add_line])
    db_session.flush()

    _publish(db_session, parent, target, cutoff)

    assert refresh_line.locked_by_run_id == refresh_candidate.run_id
    assert add_line.locked_by_run_id == add_candidate.run_id


def test_publish_rejects_prelocked_add_plan(db_session):
    cutoff, parent, target, _parents, candidates = _batch(db_session, count=0, add_count=1)
    item = _line_item(db_session, "prelocked")
    line = models.ProductionPlanLine(
        plan_id=candidates[0].source_plan_id, item_id=item.item_id,
        bucket_date=date(2026, 8, 1), qty=1, locked_by_run_id=candidates[0].run_id,
    )
    db_session.add(line)
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match="already locked"):
        _publish(db_session, parent, target, cutoff)


def test_publish_rejects_misplaced_purchase_journal_row_contract(db_session):
    cutoff, parent, target, _parents, _candidates = _batch(db_session)
    _set_purchase_journal_rows(
        db_session,
        target,
        rows=[{
            "row_key": "ledger-supply:1",
            "row_generator": "ledger_future_supply",
            "required_qty": 10.0,
            "realized_qty": 0.0,
            "open_order_covered_qty": 0.0,
            "to_order_qty": 10.0,
            "quantity": 10.0,
            "remaining_qty": 10.0,
            "run_id": 1,
            "run_ids": [1],
            "received_qty": 0.0,
        }],
    )

    with pytest.raises(ObligationRefreshPublishError, match="purchase control journal row violates Ledger fact contract"):
        _publish(db_session, parent, target, cutoff)


def test_publish_allows_buy_journal_row_contract(db_session):
    cutoff, parent, target, _parents, candidates = _batch(db_session)
    run_id = int(candidates[0].run_id)
    _set_purchase_journal_rows(
        db_session,
        target,
        rows=[{
            "row_key": "buy:1:main",
            "row_generator": "mrp_reservation",
            "required_qty": 10.0,
            "realized_qty": 2.0,
            "open_order_covered_qty": 1.0,
            "to_order_qty": 7.0,
            "quantity": 10.0,
            "remaining_qty": 7.0,
            "run_id": run_id,
            "run_ids": [run_id],
            "requirement_ids": [101],
            "reservation_ids": [201],
            "received_qty": 2.0,
            "planning_stock_pool": "main",
        }],
    )
    # If row contract drifts here, publish must fail immediately on the contract
    # gate before touching production-run locking.
    assert _publish(db_session, parent, target, cutoff).published is True


@pytest.mark.parametrize("mutation, error", [
    ("period", "period conflicts"),
    ("config", "config conflicts"),
    ("lifecycle", "terminal lifecycle"),
])
def test_publish_rejects_tampered_added_candidate_lineage(db_session, mutation, error):
    cutoff, parent, target, _parents, candidates = _batch(db_session, count=0, add_count=1)
    candidate = candidates[0]
    if mutation == "period":
        candidate.period_to = date(2026, 9, 1)
    elif mutation == "config":
        candidate.config_snapshot = {"not": "sealed"}
    else:
        candidate.pinned = True
        candidate.fixed_at = cutoff
        candidate.finished_at = cutoff
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match=error):
        _publish(db_session, parent, target, cutoff)


@pytest.mark.parametrize("mutation", ["period", "config", "lifecycle"])
def test_exact_retry_rejects_tampered_published_add_lineage(db_session, mutation):
    cutoff, parent, target, _parents, candidates = _batch(db_session, count=0, add_count=1)
    _publish(db_session, parent, target, cutoff)
    db_session.commit()
    candidate = db_session.get(models.PlanningRun, candidates[0].run_id)
    if mutation == "period":
        candidate.period_from = date(2026, 7, 31)
    elif mutation == "config":
        candidate.config_snapshot = {"changed": True}
    else:
        candidate.pinned = False
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match="mixed or partial"):
        _publish(db_session, parent, target, cutoff)


@pytest.mark.parametrize("mutation, error", [
    ("omitted", "missing or extra candidates"),
    ("extra", "missing or extra candidates"),
    ("tampered_hash", "hash conflicts"),
])
def test_publish_requires_intact_sealed_obligation_manifest(db_session, mutation, error):
    cutoff, parent, target, _parents, candidates = _batch(db_session, count=1)
    manifest = target.source_watermarks[MANIFEST_KEY]
    if mutation == "omitted":
        manifest["entries"] = []
        # Deliberately recompute the stored hash: this is a valid JSON shape
        # but it no longer describes the target candidate set.
        import hashlib
        import json
        target.source_watermarks[MANIFEST_HASH_KEY] = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
    elif mutation == "extra":
        manifest["entries"].append({
            "action": "add", "plan_id": 999999, "parent_run_id": None,
            "candidate_run_id": candidates[0].run_id + 1000,
        })
        import hashlib
        import json
        target.source_watermarks[MANIFEST_HASH_KEY] = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
    else:
        target.source_watermarks[MANIFEST_HASH_KEY] = "0" * 64
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match=error):
        _publish(db_session, parent, target, cutoff)
