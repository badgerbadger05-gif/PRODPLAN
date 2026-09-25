"""A purchase journal that keeps answering with an old supplier contour.

The bounded physical refresh used to hand the accepted future-supply rows to
the next generation as technical provenance only.  Supplier orders are mutable
1C documents, so that froze their qualification at the cutoff of whichever
generation last captured them: an order placed in 1C stayed invisible (and its
demand was offered for purchase a second time), a completed order stayed open,
and the day-dependent fields of a reused row never moved.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app import models
from app.services.item_ledger.current_execution import (
    publish_current_purchase_control_from_payload,
)
from app.services.item_ledger.future_supply_capture import (
    replace_future_supply_capture,
)
from app.services.item_ledger.future_supply_capture import (
    _CURRENT_BUSINESS_FIELDS,
    FutureSupplyEvidence,
)
from app.services.item_ledger.supplier_future_supply import (
    _SUPPLY_BUSINESS_FIELDS,
    _business_payload,
    supplier_future_supply_delta,
)
from app.services.purchase_control_journal import list_journal
from app.services.purchase_control_projection import (
    build_compact_current_purchase_control_payload,
)


POOL_BY_WAREHOUSE = {"WH-1": "default"}
_CUTOFF = datetime(2026, 9, 10, tzinfo=timezone.utc)


def _batch(db, key):
    batch = models.PhysicalImportBatch(
        batch_key=key,
        status="completed",
        cutoff=_CUTOFF,
        source_watermarks={},
        completed_at=_CUTOFF,
        source_complete=True,
    )
    db.add(batch)
    db.flush()
    return batch


def _world(db):
    """One accepted parent, one BUILDING target and one live BUY owner."""
    parent = models.LedgerGeneration(
        generation_key="supplier-staleness-parent",
        status="accepted",
        cutoff=_CUTOFF,
        accepted_at=_CUTOFF,
        source_watermarks={},
        capabilities={"physical_ledger": True, "future_supply": True},
        physical_import_batch=_batch(db, "supplier-staleness-parent-batch"),
        algorithm_version="supplier-staleness-tests",
    )
    target = models.LedgerGeneration(
        generation_key="supplier-staleness-target",
        status="building",
        cutoff=_CUTOFF,
        source_watermarks={},
        capabilities={"physical_ledger": True, "future_supply": True},
        physical_import_batch=_batch(db, "supplier-staleness-target-batch"),
        algorithm_version="supplier-staleness-tests",
    )
    db.add_all([parent, target])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))

    item = models.Item(
        item_code="SUPPLIER-STALENESS-1",
        item_name="Материал",
        item_article="ART-1",
        unit="шт",
        item_ref1c="ITEM-REF-1",
        supplier_ref1c="SUP-REF",
    )
    db.add(item)
    db.flush()
    plan = models.ProductionPlanHeader(
        name="Supplier staleness plan",
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
        replenishment_received_qty=Decimal("0"),
        realized_qty=Decimal("0"),
        lifecycle_status="active",
        owner_kind="current",
        is_current=True,
        current_identity=f"reservation:req:{requirement.id}:mode:buy",
    )
    db.add(reservation)
    db.flush()
    db.commit()
    return parent, target, item, run, reservation


def _order(db, item, *, number="ЗСНФ-001766", state="Заказан (товар в пути)", qty="6"):
    supplier = db.query(models.Supplier).filter(
        models.Supplier.supplier_ref1c == "SUP-REF"
    ).one_or_none()
    if supplier is None:
        supplier = models.Supplier(
            supplier_name="Поставщик",
            supplier_ref1c="SUP-REF",
        )
        db.add(supplier)
        db.flush()
    order = models.SupplierOrder(
        order_number=number,
        order_date=datetime(2026, 9, 5),
        order_ref1c=f"REF-{number}",
        supplier_id=supplier.supplier_id,
        document_amount=Decimal("100"),
        is_posted=True,
        order_state_key="STATE",
        order_state_name=state,
        deletion_mark=False,
        created_at=datetime(2026, 9, 5),
        updated_at=datetime(2026, 9, 5),
    )
    db.add(order)
    db.flush()
    line = models.SupplierOrderItem(
        order_id=order.order_id,
        item_id_ref=item.item_id,
        line_number=1,
        characteristic_ref1c=None,
        destination_warehouse_ref1c="WH-1",
        quantity=Decimal(qty),
        received_qty=Decimal("0"),
        remaining_qty=Decimal(qty),
        delivery_date=datetime(2026, 9, 22),
        created_at=datetime(2026, 9, 5),
    )
    db.add(line)
    db.flush()
    return order, line


def _scope(item):
    return (item.item_id, "", "", "default", "buy")


def test_supplier_order_placed_in_1c_is_a_semantic_delta_with_its_own_scope(db_session):
    parent, target, item, _run, _reservation = _world(db_session)
    assert supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    ).changed_scopes == ()

    _order(db_session, item)

    delta = supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )
    assert delta.changed_scopes == (_scope(item),)
    assert [row.source_ref for row in delta.evidence] == ["REF-ЗСНФ-001766"]
    assert parent.id  # the accepted contour itself is untouched by the query


def test_repeated_sync_without_business_change_is_not_a_delta(db_session):
    _parent, target, item, _run, _reservation = _world(db_session)
    order, line = _order(db_session, item)
    delta = supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )
    _publish_supplier_evidence(db_session, target, delta)

    # A re-synchronization which only re-stamps the mirror timestamps.
    order.updated_at = datetime(2026, 9, 24, 16, 58)
    line.updated_at = datetime(2026, 9, 24, 16, 58)
    db_session.flush()

    assert supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    ).changed_scopes == ()


def test_supplier_comparison_covers_the_whole_current_business_payload():
    """The delta compares every business field the current owner stores.

    ``source_requirement_id`` and ``open_qty_at_cutoff`` used to be missing, so
    a change of either was invisible to the tick that had to notice it.
    """
    assert set(_SUPPLY_BUSINESS_FIELDS) == set(_CURRENT_BUSINESS_FIELDS)


def test_derived_open_qty_does_not_make_an_unchanged_line_look_changed(db_session):
    """Evidence has no stored open quantity; comparing it must not flap."""
    stored = models.LedgerFutureSupplyCurrent(
        current_identity="supplier_order:REF-1:1:",
        supply_kind="supplier_order",
        item_id=1,
        planning_stock_pool="default",
        destination_warehouse_ref1c="WH-1",
        source_ref="REF-1",
        source_line_ref="1",
        ordered_qty_at_cutoff=Decimal("10.000"),
        realized_qty_at_cutoff=Decimal("4.000"),
        open_qty_at_cutoff=Decimal("6.000"),
        eta_date=date(2026, 9, 22),
        source_state_key="STATE",
        evidence_status="exact",
    )
    fresh = FutureSupplyEvidence(
        supply_kind="supplier_order",
        item_id=1,
        planning_stock_pool="default",
        destination_warehouse_ref1c="WH-1",
        source_ref="REF-1",
        source_line_ref="1",
        ordered_qty_at_cutoff=Decimal("10"),
        realized_qty_at_cutoff=Decimal("4"),
        eta_date=date(2026, 9, 22),
        source_state_key="STATE",
        evidence_status="exact",
    )

    assert _business_payload(stored) == _business_payload(fresh)


def test_completed_supplier_order_closes_its_supply_and_names_the_scope(db_session):
    _parent, target, item, _run, _reservation = _world(db_session)
    order, _line = _order(db_session, item)
    delta = supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )
    _publish_supplier_evidence(db_session, target, delta)

    order.order_state_name = "Завершен"
    db_session.flush()

    closed = supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )
    assert closed.changed_scopes == (_scope(item),)
    assert all(
        row.evidence_status != "exact"
        for row in closed.evidence
        if row.source_ref == "REF-ЗСНФ-001766"
    )


def _publish_supplier_evidence(db, target, delta):
    """Persist fresh evidence as this generation's current supply owner."""
    batch = models.LedgerBuildBatch(
        ledger_generation_id=target.id,
        stage="future_supply_capture",
        batch_key=f"future-supply-capture:g{target.id}",
        status="building",
        algorithm_version="ledger-future-supply-capture/1",
        metrics={},
    )
    db.add(batch)
    db.flush()
    replace_future_supply_capture(db, target.id, batch.id, delta.evidence)
    rows = db.query(models.LedgerFutureSupply).filter(
        models.LedgerFutureSupply.ledger_generation_id == target.id,
        models.LedgerFutureSupply.evidence_status == "exact",
    ).all()
    for row in rows:
        db.add(models.LedgerFutureSupplyCurrent(
            current_identity=row.current_identity,
            source_generation_id=target.id,
            source_capture_batch_id=batch.id,
            supply_kind=row.supply_kind,
            item_id=row.item_id,
            characteristic_ref=row.characteristic_ref,
            organization_ref=row.organization_ref,
            planning_stock_pool=row.planning_stock_pool,
            destination_warehouse_ref1c=row.destination_warehouse_ref1c,
            source_ref=row.source_ref,
            source_line_ref=row.source_line_ref,
            source_local_id=row.source_local_id,
            ordered_qty_at_cutoff=row.ordered_qty_at_cutoff,
            realized_qty_at_cutoff=row.realized_qty_at_cutoff,
            open_qty_at_cutoff=row.open_qty_at_cutoff,
            eta_date=row.eta_date,
            source_state_key=row.source_state_key,
            capture_cutoff=row.capture_cutoff,
            source_content_hash=row.source_content_hash,
            evidence_status=row.evidence_status,
        ))
    db.query(models.LedgerFutureSupply).filter(
        models.LedgerFutureSupply.ledger_generation_id == target.id
    ).delete(synchronize_session=False)
    db.flush()


def test_new_supplier_order_appears_and_reduces_to_order_on_the_next_refresh(db_session):
    parent, target, item, run, _reservation = _world(db_session)
    stale = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id],
    )
    publish_current_purchase_control_from_payload(db_session, parent.id, stale)
    db_session.flush()
    assert [row["to_order_qty"] for row in stale["rows"]] == [10.0]

    _order(db_session, item)
    delta = supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )
    batch = models.LedgerBuildBatch(
        ledger_generation_id=target.id,
        stage="future_supply_capture",
        batch_key=f"future-supply-capture:g{target.id}",
        status="building",
        algorithm_version="ledger-future-supply-capture/1",
        metrics={},
    )
    db_session.add(batch)
    db_session.flush()
    replace_future_supply_capture(db_session, target.id, batch.id, delta.evidence)

    payload = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id],
        affected_scopes=delta.changed_scopes,
        reuse_parent_current=True,
        future_supply_generation_id=target.id,
    )

    supplier_rows = [
        row for row in payload["rows"]
        if row["row_generator"] == "ledger_future_supply"
    ]
    buy_rows = [
        row for row in payload["rows"]
        if row["row_generator"] == "mrp_reservation"
    ]
    assert [row["order_number"] for row in supplier_rows] == ["ЗСНФ-001766"]
    assert [row["remaining_qty"] for row in supplier_rows] == [6.0]
    assert [row["open_order_covered_qty"] for row in buy_rows] == [6.0]
    assert [row["to_order_qty"] for row in buy_rows] == [4.0]


def _publish_supplier_journal_row(db, generation, *, eta, line_status, overdue_days):
    payload = {
        "meta": {
            "ledger_generation_id": generation.id,
            "ledger_generation": generation.id,
            "cutoff": generation.cutoff.isoformat(),
            "truth_status": "building",
            "read_only": True,
            "fact_source": "current",
            "received_qty_status": "available",
            "run_ids": [],
            "to_order_by_period": [],
            "row_count": 1,
        },
        "rows": [{
            "row_key": "ledger-supply:supplier_order:REF:1",
            "order_id": None,
            "order_number": "ЗСНФ-001700",
            "order_ref1c": "REF",
            "order_state_name": "Заказан (товар в пути)",
            "supply_phase": "in_transit",
            "item_id": 1,
            "item_code": "SUPPLIER-STALENESS-1",
            "item_name": "Материал",
            "planning_stock_pool": "default",
            "quantity": 6.0,
            "received_qty": 0.0,
            "remaining_qty": 6.0,
            "delivery_date": eta.isoformat(),
            "overdue_days": overdue_days,
            "line_status": line_status,
            "row_generator": "ledger_future_supply",
            "fact_status": "available",
            "fact_source": "ledger",
            "run_ids": [],
        }],
        "cards": {},
        "summary": {"total_rows": 1, "to_order": 0, "fact_status": "available"},
    }
    publish_current_purchase_control_from_payload(db, generation.id, payload)
    db.flush()


def test_stored_expected_row_is_served_overdue_once_its_delivery_date_passes(db_session):
    parent, _target, _item, _run, _reservation = _world(db_session)
    today = date.today()
    _publish_supplier_journal_row(
        db_session,
        parent,
        eta=today - timedelta(days=11),
        line_status="expected",
        overdue_days=0,
    )

    served = list_journal(db_session, active_only=True)

    assert [row["line_status"] for row in served["rows"]] == ["overdue"]
    assert [row["overdue_days"] for row in served["rows"]] == [11]
    assert served["summary"]["by_status"] == {"overdue": 1}
    assert served["summary"]["overdue"] == 1


def test_a_future_delivery_date_is_still_served_as_expected(db_session):
    parent, _target, _item, _run, _reservation = _world(db_session)
    _publish_supplier_journal_row(
        db_session,
        parent,
        eta=date.today() + timedelta(days=3),
        line_status="overdue",
        overdue_days=99,
    )

    served = list_journal(db_session, active_only=True)

    assert [row["line_status"] for row in served["rows"]] == ["expected"]
    assert [row["overdue_days"] for row in served["rows"]] == [0]


def test_publishing_generation_stages_its_own_supplier_contour(db_session):
    """The bounded publisher captures supply, it does not inherit it.

    A physical generation is the point at which the mutable 1C order becomes
    immutable evidence; rebinding the parent's rows as provenance kept the
    qualification of the cutoff that last captured them.
    """
    from app.services.item_ledger import physical_refresh_current_publish as publisher

    parent, target, item, _run, _reservation = _world(db_session)
    db_session.add(models.LedgerFutureSupplyCurrent(
        current_identity="wip_order:WIP-1:1",
        source_generation_id=parent.id,
        source_capture_batch_id=_completed_capture(db_session, parent).id,
        supply_kind="wip_order",
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        destination_warehouse_ref1c="WH-1",
        source_ref="WIP-1",
        source_line_ref="1",
        source_local_id="wip-1",
        ordered_qty_at_cutoff=Decimal("3"),
        realized_qty_at_cutoff=Decimal("0"),
        open_qty_at_cutoff=Decimal("3"),
        eta_date=date(2026, 9, 18),
        source_state_key="В производстве",
        capture_cutoff=parent.cutoff,
        source_content_hash="w" * 64,
        evidence_status="exact",
    ))
    db_session.flush()
    _order(db_session, item)
    delta = supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    publisher._capture_bounded_future_supply(
        db_session,
        parent=parent,
        target=target,
        supplier_evidence=delta.evidence,
    )

    staged = db_session.query(models.LedgerFutureSupply).filter(
        models.LedgerFutureSupply.ledger_generation_id == target.id,
        models.LedgerFutureSupply.evidence_status == "exact",
    ).all()
    assert sorted((row.supply_kind, row.source_ref) for row in staged) == [
        ("supplier_order", "REF-ЗСНФ-001766"),
        ("wip_order", "WIP-1"),
    ]


def _completed_capture(db, generation):
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="future_supply_capture",
        batch_key=f"future-supply-capture:g{generation.id}",
        status="completed",
        algorithm_version="ledger-future-supply-capture/1",
        metrics={},
    )
    db.add(batch)
    db.flush()
    return batch


def test_a_supplier_only_tick_is_not_treated_as_an_empty_delta(db_session):
    """No movement plus a changed 1C order is still a reason to publish.

    §57 extends freshness only for a refresh that found no semantic delta;
    §25 accepts a changed supplier order only through a new generation, so the
    bounded publisher must not reject this tick as a no-op.
    """
    import pytest

    from app.services.item_ledger import physical_refresh_current_publish as publisher

    parent, target, _item, _run, _reservation = _world(db_session)
    call = dict(
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        odata_client=None,
        source_revision=target.physical_import_batch_id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    with pytest.raises(publisher.ForwardPhysicalRefreshUnavailable) as unchanged:
        publisher.publish_forward_physical_refresh_current(
            db_session,
            delta_manifest={"rows": (), "supersessions": ()},
            **call,
        )
    assert "empty physical delta" in str(unchanged.value)

    # It now fails on a later, unrelated gate of this minimal fixture - which
    # is exactly the point: the no-op guard no longer stops it.
    with pytest.raises(Exception) as changed:
        publisher.publish_forward_physical_refresh_current(
            db_session,
            delta_manifest={
                "rows": (),
                "supersessions": (),
                "supplier_future_supply_changed": True,
            },
            **call,
        )
    assert "empty physical delta" not in str(changed.value)


def test_expected_within_seven_days_is_counted_for_the_serving_day(db_session):
    parent, _target, _item, _run, _reservation = _world(db_session)
    _publish_supplier_journal_row(
        db_session,
        parent,
        eta=date.today() + timedelta(days=3),
        line_status="overdue",
        overdue_days=99,
    )

    served = list_journal(db_session, active_only=True)

    assert served["summary"]["expected_7d"] == 1
    assert served["summary"]["overdue"] == 0


def _legacy_supplier_row(*, key, item, order_ref, qty="6"):
    """A journal row as the pre-contract publisher wrote it.

    Its identity is the technical id of a future-supply row and its payload
    carries no planning pool, so neither the row key nor the item/pool scope of
    a later bounded refresh can ever match it.
    """
    return {
        "row_key": key,
        "order_id": None,
        "order_number": order_ref.replace("REF-", ""),
        "order_ref1c": order_ref,
        "order_state_name": "Заказан (товар в пути)",
        "supply_phase": "in_transit",
        "item_id": item.item_id,
        "item_code": item.item_code,
        "item_name": item.item_name,
        "quantity": float(qty),
        "received_qty": 0.0,
        "remaining_qty": float(qty),
        "delivery_date": "2026-09-22",
        "overdue_days": 0,
        "line_status": "expected",
        "row_generator": "ledger_future_supply",
        "fact_status": "available",
        "fact_source": "ledger",
        "run_ids": [],
    }


def _publish_parent_with_legacy_rows(db, parent, target, run, item, legacy_rows):
    ordinary = build_compact_current_purchase_control_payload(
        db,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id],
    )
    payload = {
        **ordinary,
        "rows": [*ordinary["rows"], *legacy_rows],
    }
    publish_current_purchase_control_from_payload(db, parent.id, payload)
    db.flush()
    return payload


def test_recapture_supersedes_rows_published_under_the_old_row_identity(db_session):
    """One journal row per supplier order line, and none for a closed order."""
    parent, target, item, run, _reservation = _world(db_session)
    open_order, open_line = _order(db_session, item)
    closed_order, _closed_line = _order(
        db_session, item, number="ЗСНФ-001565", state="Завершен", qty="4",
    )
    _publish_parent_with_legacy_rows(
        db_session, parent, target, run, item,
        [
            _legacy_supplier_row(
                key="ledger-supply:34560614", item=item,
                order_ref=open_order.order_ref1c,
            ),
            _legacy_supplier_row(
                key="ledger-supply:34572633", item=item,
                order_ref=closed_order.order_ref1c, qty="4",
            ),
        ],
    )

    delta = supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )
    _stage_supplier_capture(db_session, target, delta)
    payload = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id],
        affected_scopes=delta.changed_scopes,
        reuse_parent_current=True,
        future_supply_generation_id=target.id,
    )

    supplier_rows = [
        row for row in payload["rows"]
        if row["row_generator"] == "ledger_future_supply"
    ]
    assert [row["row_key"] for row in supplier_rows] == [
        "ledger-supply:supplier_order:"
        f"{open_order.order_ref1c}:1:supplier_order_item:{open_line.item_id}"
    ]
    assert [row["order_number"] for row in supplier_rows] == ["ЗСНФ-001766"]
    assert not [
        row for row in payload["rows"]
        if str(row.get("order_ref1c") or "") == closed_order.order_ref1c
    ]


def test_a_stock_only_tick_still_closes_legacy_supplier_rows(db_session):
    """No BUY scope at all is not a reason to keep an unsupported identity."""
    parent, target, item, run, _reservation = _world(db_session)
    order, line = _order(db_session, item)
    delta = supplier_future_supply_delta(
        db_session,
        target.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )
    _stage_supplier_capture(db_session, target, delta)
    # The parent already describes this very order, but under the old identity.
    _publish_parent_with_legacy_rows(
        db_session, parent, target, run, item,
        [_legacy_supplier_row(
            key="ledger-supply:34560614", item=item, order_ref=order.order_ref1c,
        )],
    )

    payload = build_compact_current_purchase_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        accepted_run_ids=[run.run_id],
        affected_scopes=(),
        reuse_parent_current=True,
        future_supply_generation_id=target.id,
    )

    assert [
        row["row_key"] for row in payload["rows"]
        if row["row_generator"] == "ledger_future_supply"
    ] == [
        "ledger-supply:supplier_order:"
        f"{order.order_ref1c}:1:supplier_order_item:{line.item_id}"
    ]


def _stage_supplier_capture(db, target, delta):
    batch = models.LedgerBuildBatch(
        ledger_generation_id=target.id,
        stage="future_supply_capture",
        batch_key=f"future-supply-capture:g{target.id}",
        status="building",
        algorithm_version="ledger-future-supply-capture/1",
        metrics={},
    )
    db.add(batch)
    db.flush()
    replace_future_supply_capture(db, target.id, batch.id, delta.evidence)
    return batch
