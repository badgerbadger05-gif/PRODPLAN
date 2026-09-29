from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import models
from app.services.purchase_control_projection import (
    PurchaseControlCompactPayloadError,
    build_compact_current_purchase_control_payload,
    validate_compact_current_purchase_control_payload,
)
from app.services.item_ledger.current_execution import (
    load_current_execution_rows,
    publish_current_purchase_control_from_payload,
)
from app.services.item_ledger.current_replenishment import apply_current_replenishment
from app.services.item_ledger.historical_replay_core import Reserve
from app.services.item_ledger import physical_refresh_current_publish as publisher
from app.services import purchase_control_projection as purchase_projection


def _batch(key, cutoff):
    return models.PhysicalImportBatch(
        batch_key=key,
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
        source_complete=True,
    )


def _world(db, *, item_count=1):
    parent_cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    parent_batch = _batch("compact-purchase-parent-batch", parent_cutoff)
    target1_batch = _batch("compact-purchase-target-1-batch", parent_cutoff)
    target2_batch = _batch("compact-purchase-target-2-batch", parent_cutoff)
    db.add_all([parent_batch, target1_batch, target2_batch])
    db.flush()
    parent = models.LedgerGeneration(
        generation_key="compact-purchase-parent",
        status="accepted",
        cutoff=parent_cutoff,
        accepted_at=parent_cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=parent_batch,
        algorithm_version="compact-purchase-tests",
    )
    target1 = models.LedgerGeneration(
        generation_key="compact-purchase-target-1",
        status="building",
        cutoff=parent_cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=target1_batch,
        algorithm_version="compact-purchase-tests",
    )
    target2 = models.LedgerGeneration(
        generation_key="compact-purchase-target-2",
        status="building",
        cutoff=parent_cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=target2_batch,
        algorithm_version="compact-purchase-tests",
    )
    db.add_all([parent, target1, target2])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    capture = models.LedgerBuildBatch(
        ledger_generation_id=parent.id,
        stage="future_supply_capture",
        batch_key="compact-purchase-future-capture",
        status="completed",
        algorithm_version="compact-purchase-tests",
        metrics={},
    )
    db.add(capture)
    db.flush()

    rows = []
    for index in range(item_count):
        item = models.Item(
            item_code=f"COMPACT-PURCHASE-{index}",
            item_name="Compact purchase item",
            item_article=f"COMPACT-{index}",
            unit="шт",
            accounting_price=Decimal("12.50"),
            supplier_ref1c="SUP-COMPACT",
        )
        db.add(item)
        db.flush()
        plan = models.ProductionPlanHeader(
            name=f"Compact purchase plan {index}",
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 30),
            status="fixed",
        )
        db.add(plan)
        db.flush()
        run = models.PlanningRun(
            status="FIXED_SNAPSHOT",
            config_snapshot={},
            source_plan_id=plan.id,
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 30),
            ledger_generation_id=parent.id,
            ledger_cutoff=parent.cutoff,
        )
        db.add(run)
        db.flush()
        requirement = models.MrpRequirement(
            run_id=run.run_id,
            item_id=item.item_id,
            total_required_qty=Decimal("10"),
            net_required_qty=Decimal("10"),
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 30),
            bom_level=1,
            planning_stock_pool="default",
            characteristic_ref="",
            organization_ref="",
            freeze_version=1,
        )
        db.add(requirement)
        db.flush()
        reservation = models.ReservationEntry(
            ledger_generation_id=parent.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="default",
            run_id=run.run_id,
            freeze_version=1,
            requirement_id=requirement.id,
            priority_period_from=date(2026, 9, 1),
            priority_period_to=date(2026, 9, 30),
            realization_mode="buy",
            reserved_qty=Decimal("10"),
            replenishment_required_qty=Decimal("10"),
            replenishment_received_qty=Decimal("2"),
            realized_qty=Decimal("2"),
            lifecycle_status="active",
            owner_kind="current",
            is_current=True,
            current_identity=f"reservation:req:{requirement.id}:mode:buy",
        )
        db.add(reservation)
        db.flush()
        rows.append((item, run, reservation, capture))
    db.commit()
    return parent, target1, target2, rows


def _build(db, parent, target, rows, *, scopes=None):
    return build_compact_current_purchase_control_payload(
        db,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id for _item, run, _reservation, _capture in rows],
        affected_scopes=scopes,
    )


def _add_current_supply(db, parent, item, capture, *, eta=date(2026, 9, 20)):
    db.add(models.LedgerFutureSupplyCurrent(
        current_identity="supplier_order:COMPACT-ORDER:1",
        source_generation_id=parent.id,
        source_capture_batch_id=capture.id,
        supply_kind="supplier_order",
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        destination_warehouse_ref1c="WH",
        source_ref="COMPACT-ORDER",
        source_line_ref="1",
        source_local_id="compact-1",
        ordered_qty_at_cutoff=Decimal("5"),
        realized_qty_at_cutoff=Decimal("1"),
        open_qty_at_cutoff=Decimal("4"),
        eta_date=eta,
        source_state_key="В закупку",
        capture_cutoff=parent.cutoff,
        source_content_hash="s" * 64,
        evidence_status="exact",
    ))
    db.flush()


def test_compact_purchase_next_target_parity_has_stable_payload_and_no_audit(db_session):
    parent, target1, target2, rows = _world(db_session)
    before_changes = db_session.query(models.CurrentExecutionChange).count()
    before_work = db_session.query(models.ReplenishmentWorkItem).count()

    first = _build(db_session, parent, target1, rows)
    second = _build(db_session, parent, target2, rows)

    assert first["rows"] == second["rows"]
    assert first["rows"][0]["current_reservation_identities"] == [
        rows[0][2].current_identity
    ]
    assert db_session.query(models.CurrentExecutionChange).count() == before_changes
    assert db_session.query(models.ReservationCurrentChange).count() == 0
    assert db_session.query(models.ReplenishmentWorkItem).count() == before_work == 0
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_compact_purchase_affected_buy_scope_isolated_and_has_no_staging_owner(db_session):
    parent, target1, _target2, rows = _world(db_session, item_count=2)
    scope = (
        rows[0][0].item_id,
        "",
        "",
        "default",
        "buy",
    )
    payload = _build(db_session, parent, target1, rows, scopes=(scope,))

    assert [row["item_id"] for row in payload["rows"]] == [rows[0][0].item_id]
    assert payload["rows"][0]["reservation_ids"] == [rows[0][2].id]
    assert db_session.query(models.ReplenishmentWorkItem).count() == 0
    validate_compact_current_purchase_control_payload(payload, target1)


def test_bounded_purchase_reuses_complete_parent_for_stock_only_tick_without_coverage_scan(
    db_session, monkeypatch
):
    parent, target1, target2, rows = _world(db_session)
    initial = _build(db_session, parent, target1, rows)
    publish_current_purchase_control_from_payload(db_session, parent.id, initial)
    db_session.flush()

    def fail_if_recomputed(*args, **kwargs):
        raise AssertionError("stock-only bounded purchase refresh recomputed coverage")

    monkeypatch.setattr(
        purchase_projection,
        "open_supplier_coverage_by_reservation",
        fail_if_recomputed,
    )
    payload = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target2.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[rows[0][1].run_id],
        affected_scopes=(),
        reuse_parent_current=True,
    )

    current_rows = load_current_execution_rows(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )
    assert payload["meta"]["bounded_reuse"] is True
    assert payload["meta"]["reused_row_count"] == len(current_rows)
    assert payload["rows"] == [dict(row.payload) for row in current_rows]
    assert payload["cards"] == {}
    validate_compact_current_purchase_control_payload(payload, target2)


def test_accepted_buy_correction_invalidates_purchase_scope_only_on_change(
    db_session, monkeypatch,
):
    prior_cutoff = datetime(2026, 9, 9, tzinfo=timezone.utc)
    prior_batch = _batch("compact-purchase-prior-batch", prior_cutoff)
    db_session.add(prior_batch)
    db_session.flush()
    prior = models.LedgerGeneration(
        generation_key="compact-purchase-prior",
        status="accepted",
        cutoff=prior_cutoff,
        accepted_at=prior_cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=prior_batch,
        algorithm_version="compact-purchase-tests",
    )
    db_session.add(prior)
    db_session.flush()
    parent, target, next_target, rows = _world(db_session, item_count=2)
    initial = _build(db_session, parent, target, rows)
    publish_current_purchase_control_from_payload(db_session, parent.id, initial)
    db_session.flush()
    item, run, reservation, _capture = rows[0]
    scope = (item.item_id, "", "", "default", "buy")
    reserve = Reserve(
        reserve_id=str(reservation.id), item_id=item.item_id, mode="buy",
        reserved_qty=Decimal("10"), due_date=reservation.priority_period_to,
        plan_period_from=reservation.priority_period_from,
        plan_period_to=reservation.priority_period_to,
        run_id=run.run_id, requirement_id=reservation.requirement_id,
        planning_stock_pool="default",
    )
    purchase_scope = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="purchase_control_journal", scope_key="purchase:all-live-plans"
    ).one()
    # A semantic no-op physical tick can leave a ready read model from an
    # earlier generation.  Invalidation does not rewrite its source id.
    purchase_scope.source_generation_id = prior.id
    db_session.flush()
    assert purchase_scope.result_ready is True

    first = apply_current_replenishment(
        db_session, generation_id=parent.id, source_key="supplier-receipts",
        source_revision=parent.id, facts=(), reserves=(reserve,),
        complete_scope=True, distribution_scope=scope,
    )
    db_session.flush()
    assert reservation.replenishment_received_qty == Decimal("0")
    assert first.changed_pairs == 0  # Quantity changed without an allocation diff.
    assert purchase_scope.result_ready is False

    second = apply_current_replenishment(
        db_session, generation_id=parent.id, source_key="supplier-receipts",
        source_revision=parent.id, facts=(), reserves=(reserve,),
        complete_scope=True, distribution_scope=scope,
    )
    assert second.idempotent is True
    assert second.changed_pairs == 0
    assert purchase_scope.result_ready is False

    # A real physical delta may arrive before the no-op repair.  Its bounded
    # purchase builder must consume the invalidation and use current owners.
    refreshed = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=next_target.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id for _item, run, _res, _capture in rows],
        affected_scopes=(),
        reuse_parent_current=True,
    )
    by_item = {row["item_id"]: row for row in refreshed["rows"]}
    assert refreshed["meta"]["bounded_reuse"] is False
    assert by_item[item.item_id]["received_qty"] == 0
    assert by_item[rows[1][0].item_id]["received_qty"] == 2

    # The worker's no-op tick repairs the same invalidated manifest, using
    # the real purchase builder.  Other heavy scope builders are unrelated.
    monkeypatch.setattr(
        publisher, "build_compact_current_assembly_payload",
        lambda *a, **kw: SimpleNamespace(
            queue_rows=(), readiness_rows=(), readiness_metrics={},
        ),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_drum_payload",
        lambda *a, **kw: SimpleNamespace(rows=(), metrics={}),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_shelf_payload",
        lambda *a, **kw: SimpleNamespace(rows=(), metrics={}),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_production_control_payload",
        lambda *a, **kw: {"rows": [], "meta": {"read_only": True, "fact_source": "current"}},
    )
    monkeypatch.setattr(publisher, "_build_obligation_view_payloads", lambda *a, **kw: ({}, {}))
    monkeypatch.setattr(
        publisher, "publish_current_obligation_views_from_generation",
        lambda db, generation_id, *, purchase_payload, **kw: {
            "purchase_control_journal": publish_current_purchase_control_from_payload(
                db, generation_id, purchase_payload
            ),
        },
    )
    repair = publisher.repair_current_execution_scopes_from_pointer(
        db_session,
        pointer_generation_id=parent.id,
        payload_boundary_generation_id=next_target.id,
        source_revision="no-op-tick",
    )
    assert repair is not None
    published = load_current_execution_rows(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )
    published_by_item = {row.payload["item_id"]: row.payload for row in published}
    assert published_by_item[item.item_id]["received_qty"] == 0
    assert published_by_item[rows[1][0].item_id]["received_qty"] == 2
    changes = db_session.query(models.CurrentExecutionChange).count()
    repeated = apply_current_replenishment(
        db_session, generation_id=parent.id, source_key="supplier-receipts",
        source_revision=parent.id, facts=(), reserves=(reserve,),
        complete_scope=True, distribution_scope=scope,
    )
    assert repeated.idempotent is True
    assert purchase_scope.result_ready is True
    assert publisher.repair_current_execution_scopes_from_pointer(
        db_session,
        pointer_generation_id=parent.id,
        payload_boundary_generation_id=next_target.id,
        source_revision="no-op-tick",
    ) is None
    assert db_session.query(models.CurrentExecutionChange).count() == changes


def test_bounded_purchase_recomputes_only_affected_item_and_reuses_neighbor(
    db_session, monkeypatch
):
    parent, target1, target2, rows = _world(db_session, item_count=2)
    initial = _build(db_session, parent, target1, rows)
    publish_current_purchase_control_from_payload(db_session, parent.id, initial)
    db_session.flush()
    scope = (rows[0][0].item_id, "", "", "default", "buy")
    original = purchase_projection.open_supplier_coverage_by_reservation
    original_cards = purchase_projection._build_supplier_card_rows
    seen: list[set[int]] = []

    def bounded_only(db, generation_id, entries, *, affected_scopes=None, **kwargs):
        seen.append({int(item.item_id) for _work, _reservation, item in entries})
        assert affected_scopes == (scope,)
        return original(
            db,
            generation_id,
            entries,
            affected_scopes=affected_scopes,
        )

    monkeypatch.setattr(
        purchase_projection,
        "open_supplier_coverage_by_reservation",
        bounded_only,
    )

    def bounded_cards(db, generation, *, affected_scopes=None, cutoff_date=None, **kwargs):
        assert affected_scopes == (scope,)
        return original_cards(
            db,
            generation,
            affected_scopes=affected_scopes,
            cutoff_date=cutoff_date,
            **kwargs,
        )

    monkeypatch.setattr(
        purchase_projection,
        "_build_supplier_card_rows",
        bounded_cards,
    )
    payload = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target2.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id for _item, run, _reservation, _capture in rows],
        affected_scopes=(scope,),
        reuse_parent_current=True,
    )
    assert seen == [{rows[0][0].item_id}]
    by_item = {int(row["item_id"]): row for row in payload["rows"]}
    assert set(by_item) == {rows[0][0].item_id, rows[1][0].item_id}
    parent_rows = {
        int(row.payload["item_id"]): dict(row.payload)
        for row in load_current_execution_rows(
            db_session,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
    }
    assert by_item[rows[1][0].item_id] == parent_rows[rows[1][0].item_id]
    assert payload["meta"]["reused_row_count"] == 1
    validate_compact_current_purchase_control_payload(payload, target2)


def test_bounded_purchase_normalises_legacy_staged_parent_buy_row_once(db_session):
    parent, target1, target2, rows = _world(db_session)
    initial = _build(db_session, parent, target1, rows)
    legacy = deepcopy(initial)
    legacy_row = legacy["rows"][0]
    old_reservation_id = 987654321
    legacy_row["reservation_ids"] = [old_reservation_id]
    legacy_row.pop("current_reservation_identities", None)
    for field_name in ("slices", "horizon_buckets"):
        for value in legacy_row.get(field_name, []) or []:
            value["reservation_id"] = old_reservation_id
            value["work_item_id"] = 123456789
            value.pop("current_identity", None)
    for value in legacy_row.get("materialization_input", {}).get("slices", []) or []:
        value["reservation_id"] = old_reservation_id
        value["work_item_id"] = 123456789
    publish_current_purchase_control_from_payload(db_session, parent.id, legacy)
    db_session.flush()

    first = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target1.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[rows[0][1].run_id],
        affected_scopes=(),
        reuse_parent_current=True,
    )
    second = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target2.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[rows[0][1].run_id],
        affected_scopes=(),
        reuse_parent_current=True,
    )
    row = first["rows"][0]
    assert row["reservation_ids"] == [rows[0][2].id]
    assert row["current_reservation_identities"] == [rows[0][2].current_identity]
    assert all("work_item_id" not in value for value in row["slices"])
    assert all("work_item_id" not in value for value in row["horizon_buckets"])
    assert all(
        "work_item_id" not in value
        for value in row["materialization_input"]["slices"]
    )
    assert first["rows"] == second["rows"]
    validate_compact_current_purchase_control_payload(first, target1)


def test_compact_purchase_reads_current_supplier_evidence_without_generation_scan(db_session):
    parent, target, _target2, rows = _world(db_session)
    item, _run, _reservation, capture = rows[0]
    _add_current_supply(db_session, parent, item, capture)

    payload = _build(db_session, parent, target, rows)
    supplier_rows = [
        row for row in payload["rows"]
        if row.get("row_generator") == "ledger_future_supply"
    ]
    assert len(supplier_rows) == 1
    assert supplier_rows[0]["row_key"] == "ledger-supply:supplier_order:COMPACT-ORDER:1"
    assert supplier_rows[0]["remaining_qty"] == 4.0


def test_compact_purchase_allows_empty_and_supplier_only_scopes(db_session):
    parent, target, _target2, rows = _world(db_session)
    reservation = rows[0][2]
    reservation.is_current = False
    db_session.flush()
    empty = _build(db_session, parent, target, rows)
    assert empty["rows"] == []
    validate_compact_current_purchase_control_payload(empty, target)

    reservation.is_current = True
    reservation.owner_kind = "current"
    reservation.is_current = False
    db_session.flush()
    item, _run, _owner, capture = rows[0]
    _add_current_supply(db_session, parent, item, capture)
    scope = (item.item_id, "", "", "default", "buy")
    supplier_only = _build(db_session, parent, target, rows, scopes=(scope,))
    assert [row["row_generator"] for row in supplier_only["rows"]] == [
        "ledger_future_supply"
    ]
    validate_compact_current_purchase_control_payload(supplier_only, target)


def test_compact_purchase_uses_target_date_for_supplier_overdue_status(db_session):
    parent, target, _target2, rows = _world(db_session)
    target.cutoff = datetime(2026, 9, 12, tzinfo=timezone.utc)
    item, _run, _reservation, capture = rows[0]
    _add_current_supply(db_session, parent, item, capture, eta=date(2026, 9, 11))
    payload = _build(db_session, parent, target, rows)
    supplier_row = next(
        row for row in payload["rows"]
        if row.get("row_generator") == "ledger_future_supply"
    )
    assert supplier_row["overdue_days"] == 1


def test_compact_purchase_stale_parent_and_ambiguous_owner_fail_closed(db_session):
    parent, target, _target2, rows = _world(db_session)
    pointer = db_session.get(models.PlanningTruthState, 1)
    pointer.current_generation_id = target.id
    with pytest.raises(PurchaseControlCompactPayloadError, match="current truth"):
        _build(db_session, parent, target, rows)

    pointer.current_generation_id = parent.id
    rows[0][2].current_identity = "not-canonical"
    with pytest.raises(PurchaseControlCompactPayloadError, match="ambiguous stable"):
        _build(db_session, parent, target, rows)


def test_compact_purchase_duplicate_distribution_owner_fails_closed(db_session):
    parent, target, _target2, rows = _world(db_session)
    item, run, _owner, _capture = rows[0]
    run2 = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=None,
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        ledger_generation_id=parent.id,
        ledger_cutoff=parent.cutoff,
    )
    db_session.add(run2)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run2.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal("4"),
        net_required_qty=Decimal("4"),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        bom_level=1,
        planning_stock_pool="default",
        characteristic_ref="ALT",
        organization_ref="",
        freeze_version=1,
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=parent.id,
        item_id=item.item_id,
        characteristic_ref="ALT",
        organization_ref="",
        planning_stock_pool="default",
        run_id=run2.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 9, 1),
        priority_period_to=date(2026, 9, 30),
        realization_mode="buy",
        reserved_qty=Decimal("4"),
        replenishment_required_qty=Decimal("4"),
        replenishment_received_qty=Decimal("0"),
        lifecycle_status="active",
        owner_kind="current",
        is_current=True,
        current_identity=f"reservation:req:{requirement.id}:mode:buy",
    ))
    db_session.flush()
    rows.append((item, run2, db_session.query(models.ReservationEntry).filter(
        models.ReservationEntry.requirement_id == requirement.id
    ).one(), _capture))
    with pytest.raises(PurchaseControlCompactPayloadError, match="ambiguous characteristic"):
        _build(db_session, parent, target, rows)


def test_compact_purchase_validator_rejects_staged_work_identity(db_session):
    parent, target, _target2, rows = _world(db_session)
    payload = _build(db_session, parent, target, rows)
    payload["rows"][0]["slices"][0]["work_item_id"] = -999
    with pytest.raises(PurchaseControlCompactPayloadError, match="staged work"):
        validate_compact_current_purchase_control_payload(payload, target)


def _publish_empty_parent_scope(db_session, parent, template):
    """Publish a complete-but-empty accepted purchase scope, as a migration did."""

    empty = deepcopy(template)
    empty["rows"] = []
    empty["cards"] = {}
    empty["meta"] = {**empty["meta"], "row_count": 0}
    empty["summary"] = {"total_rows": 0, "to_order": 0, "fact_status": "available"}
    publish_current_purchase_control_from_payload(db_session, parent.id, empty)
    db_session.flush()
    assert load_current_execution_rows(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    ) == []


def test_bounded_purchase_bootstraps_complete_scope_when_parent_scope_is_empty(db_session):
    """A migrated stand starts with an empty parent scope and live BUY owners."""

    parent, target1, target2, rows = _world(db_session, item_count=2)
    complete = _build(db_session, parent, target1, rows)
    _publish_empty_parent_scope(db_session, parent, complete)

    scope = (rows[0][0].item_id, "", "", "default", "buy")
    payload = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target2.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id for _item, run, _reservation, _capture in rows],
        affected_scopes=(scope,),
        reuse_parent_current=True,
    )

    assert payload["meta"]["bootstrap"] is True
    assert payload["meta"]["bounded_reuse"] is False
    assert payload["meta"]["reused_row_count"] == 0
    # The complete scope is rebuilt, not the single affected BUY scope.
    assert {int(row["item_id"]) for row in payload["rows"]} == {
        rows[0][0].item_id, rows[1][0].item_id
    }
    assert [row["row_key"] for row in payload["rows"]] == [
        row["row_key"] for row in complete["rows"]
    ]
    validate_compact_current_purchase_control_payload(payload, target2)


def test_repair_path_bootstraps_empty_parent_scope_without_affected_scopes(db_session):
    """``repair_current_execution_scopes_from_pointer`` calls the same builder."""

    parent, target1, target2, rows = _world(db_session, item_count=2)
    complete = _build(db_session, parent, target1, rows)
    _publish_empty_parent_scope(db_session, parent, complete)

    payload = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target2.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id for _item, run, _reservation, _capture in rows],
        affected_scopes=(),
        reuse_parent_current=True,
    )

    assert payload["meta"]["bootstrap"] is True
    assert payload["meta"]["bounded_reuse"] is False
    assert len(payload["rows"]) == len(complete["rows"])
    validate_compact_current_purchase_control_payload(payload, target2)


def test_empty_parent_scope_without_live_buy_owners_is_reused_not_bootstrapped(db_session):
    parent, target1, target2, rows = _world(db_session)
    complete = _build(db_session, parent, target1, rows)
    _publish_empty_parent_scope(db_session, parent, complete)
    for _item, _run, reservation, _capture in rows:
        reservation.lifecycle_status = "closed"
    db_session.flush()

    payload = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target2.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[rows[0][1].run_id],
        affected_scopes=(),
        reuse_parent_current=True,
    )

    assert payload["meta"]["bootstrap"] is False
    assert payload["meta"]["bounded_reuse"] is True
    assert payload["rows"] == []


def test_bounded_purchase_with_populated_parent_scope_still_reuses(db_session):
    parent, target1, target2, rows = _world(db_session)
    initial = _build(db_session, parent, target1, rows)
    publish_current_purchase_control_from_payload(db_session, parent.id, initial)
    db_session.flush()

    payload = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target2.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[rows[0][1].run_id],
        affected_scopes=(),
        reuse_parent_current=True,
    )

    assert payload["meta"]["bootstrap"] is False
    assert payload["meta"]["bounded_reuse"] is True
    assert payload["meta"]["reused_row_count"] == len(payload["rows"]) > 0


def test_parent_buy_rows_without_current_owner_report_the_full_missing_count(
    db_session,
):
    """A whole-scope promotion failure must not read as a handful of stragglers.

    The bounded reuse path resolves every parent BUY row back to its stable
    current owner.  When an obligation refresh has not promoted those owners
    yet, *every* requirement is missing; the message used to print only the
    first eight with no size, so the operator triaged a scope-wide hole as a
    local one.
    """
    parent, _target1, target2, rows = _world(db_session, item_count=10)
    initial = _build(db_session, parent, _target1, rows)
    publish_current_purchase_control_from_payload(db_session, parent.id, initial)
    db_session.flush()

    requirement_ids = sorted(
        int(reservation.requirement_id)
        for _item, _run, reservation, _capture in rows
    )
    for _item, _run, reservation, _capture in rows:
        reservation.is_current = False
        reservation.owner_kind = "building"
    db_session.flush()

    with pytest.raises(PurchaseControlCompactPayloadError) as excinfo:
        build_compact_current_purchase_control_payload(
            db_session,
            target_generation_id=target2.id,
            parent_generation_id=parent.id,
            accepted_run_ids=[run.run_id for _item, run, _res, _cap in rows],
            affected_scopes=(),
            reuse_parent_current=True,
        )

    message = str(excinfo.value)
    assert "current BUY owner is missing for requirements" in message
    assert f"({len(requirement_ids)} total)" in message
    # The head stays truncated at eight, so the scope-wide size is only
    # reachable through the reported total.
    assert f"{requirement_ids[:8]} ... ({len(requirement_ids)} total)" in message
