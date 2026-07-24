"""Contracts for freezing sealed, unpublished PlanningRun candidates.

The historical tests in this module exercised ``refreeze_active_snapshots``:
that operation rewrote published runs from mutable ``Item.stock_qty`` and
legacy order counters.  It is deliberately retired.  These tests retain the
important arithmetic, FIFO/shared-pool, isolation and retry guards against the
current BUILDING Ledger candidate contract.
"""

from datetime import date, datetime, timezone
from hashlib import sha256
import json

import pytest

from app import models
from app.services.mrp_freeze import (
    LedgerPoolUnavailable,
    build_shared_pools,
    freeze_candidate_snapshots,
    pool_key_for,
    refreeze_active_snapshots,
)


CUTOFF = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)


def _seal(target, parents, candidates):
    entries = [
        {
            "action": "refresh",
            "plan_id": int(parent.source_plan_id),
            "parent_run_id": int(parent.run_id),
            "candidate_run_id": int(candidate.run_id),
        }
        for parent, candidate in zip(parents, candidates)
    ]
    payload = {
        "version": 1,
        "entries": sorted(entries, key=lambda row: (row["plan_id"], row["action"])),
        "add_request": {
            "plan_ids": [],
            "horizon_days": None,
            "config_version_id": None,
            "config_snapshot": {},
        },
    }
    target.source_watermarks = {
        **dict(target.source_watermarks or {}),
        "obligation_refresh_manifest": payload,
        "obligation_refresh_manifest_hash": sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }


def _freeze_candidate_generation(db, *, suffix: str = "scope"):
    physical = models.PhysicalImportBatch(
        batch_key=f"freeze-physical-{suffix}",
        status="completed",
        cutoff=CUTOFF,
        source_watermarks={},
        completed_at=CUTOFF,
    )
    generation = models.LedgerGeneration(
        generation_key=f"freeze-target-{suffix}",
        status="building",
        cutoff=CUTOFF,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=physical,
        algorithm_version="tests",
    )
    db.add_all([physical, generation])
    db.flush()
    return generation


def _item(db, code, *, method="Покупка"):
    row = models.Item(
        item_code=code,
        item_name=code,
        item_article=code,
        unit="шт",
        # Deliberately poisonous: candidate freeze must not read this legacy
        # field.  Authoritative stock is written only to target StockBin below.
        stock_qty=9999,
        replenishment_method=method,
        replenishment_time=3 if method == "Покупка" else 0,
        status="active",
    )
    db.add(row)
    db.flush()
    return row


def _bom(db, parent, child, qty=1):
    spec = models.Specification(
        spec_name=f"Spec {parent.item_code}",
        spec_ref1c=f"spec-{parent.item_code}",
    )
    db.add(spec)
    db.flush()
    db.add(models.DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(
        models.SpecComponent(
            spec_id=spec.spec_id, item_id=child.item_id, quantity=qty
        )
    )
    db.flush()


def _world(db, demands, *, root=None, stock=None, future=None):
    physical = models.PhysicalImportBatch(
        batch_key=f"freeze-physical-{id(demands)}",
        status="completed",
        cutoff=CUTOFF,
        source_watermarks={},
        completed_at=CUTOFF,
    )
    accepted = models.LedgerGeneration(
        generation_key=f"freeze-parent-{id(demands)}",
        status="accepted",
        cutoff=CUTOFF,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=physical,
        algorithm_version="tests",
        accepted_at=CUTOFF,
    )
    target = models.LedgerGeneration(
        generation_key=f"freeze-target-{id(demands)}",
        status="building",
        cutoff=CUTOFF,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="tests",
    )
    db.add_all([physical, accepted, target])
    db.flush()
    target.source_watermarks = {
        "generation_kind": "obligation_refresh",
        "parent_generation_id": accepted.id,
    }
    db.add(models.PlanningTruthState(id=1, current_generation_id=accepted.id))
    db.flush()

    if root is None:
        root = _item(db, "ROOT")
    for item, qty in (stock or {}).items():
        db.add(
            models.StockBin(
                ledger_generation_id=target.id,
                item_id=item.item_id,
                characteristic_ref="",
                organization_ref="",
                warehouse_ref1c="",
                on_hand=qty,
            )
        )
    capture = models.LedgerBuildBatch(
        ledger_generation_id=target.id,
        stage="snapshot_build",
        batch_key=f"freeze-supply-{target.id}",
        status="completed",
        algorithm_version="tests",
        metrics={},
    )
    db.add(capture)
    db.flush()
    for index, (item, qty) in enumerate((future or {}).items(), start=1):
        db.add(
            models.LedgerFutureSupply(
                ledger_generation_id=target.id,
                supply_kind="supplier_order",
                item_id=item.item_id,
                characteristic_ref="",
                organization_ref="",
                planning_stock_pool="default",
                destination_warehouse_ref1c="WH",
                source_ref=f"supply-{index}",
                source_line_ref="1",
                ordered_qty_at_cutoff=qty,
                realized_qty_at_cutoff=0,
                open_qty_at_cutoff=qty,
                eta_date=date(2026, 8, 1),
                source_state_key="open",
                capture_cutoff=CUTOFF,
                source_content_hash=f"{index:064d}",
                capture_batch_id=capture.id,
                evidence_status="exact",
            )
        )

    parents, candidates, lines = [], [], []
    for index, qty in enumerate(demands, start=1):
        month = 7 + index
        plan = models.ProductionPlanHeader(
            name=f"plan-{index}",
            period_from=date(2026, month, 1),
            period_to=date(2026, month, 28),
            status="fixed",
        )
        db.add(plan)
        db.flush()
        line = models.ProductionPlanLine(
            plan_id=plan.id,
            item_id=root.item_id,
            bucket_date=plan.period_from,
            qty=qty,
        )
        parent = models.PlanningRun(
            status="FIXED_SNAPSHOT",
            ledger_generation_id=accepted.id,
            source_plan_id=plan.id,
            period_from=plan.period_from,
            period_to=plan.period_to,
            config_snapshot={},
            started_at=CUTOFF,
            fixed_at=CUTOFF,
            finished_at=CUTOFF,
            pinned=True,
            active_freeze_version=7,
        )
        db.add_all([line, parent])
        db.flush()
        candidate = models.PlanningRun(
            status="BUILDING_SNAPSHOT",
            ledger_generation_id=target.id,
            prior_run_id=parent.run_id,
            source_plan_id=plan.id,
            period_from=plan.period_from,
            period_to=plan.period_to,
            config_snapshot={},
            started_at=CUTOFF,
            pinned=False,
        )
        db.add(candidate)
        db.flush()
        parents.append(parent)
        candidates.append(candidate)
        lines.append(line)
    _seal(target, parents, candidates)
    db.flush()
    return accepted, target, root, parents, candidates, lines


def _freeze(db, accepted, target, candidates):
    return freeze_candidate_snapshots(
        db,
        parent_generation_id=accepted.id,
        target_generation_id=target.id,
        candidate_run_ids=[row.run_id for row in candidates],
    )


def _req(db, run, item):
    return db.query(models.MrpRequirement).filter_by(
        run_id=run.run_id, item_id=item.item_id
    ).one()


def _purchase(db, run, item):
    return sum(
        float(row.qty)
        for row in db.query(models.PlannedPurchase).filter_by(
            run_id=run.run_id, item_id=item.item_id
        )
    )


def test_candidates_consume_physical_and_future_shared_pool_once_in_fifo_order(db_session):
    item = _item(db_session, "FIFO")
    accepted, target, _, parents, candidates, lines = _world(
        db_session, [10, 20], root=item, stock={item: 15}, future={item: 2}
    )
    parent_state = [(r.status, r.active_freeze_version) for r in parents]

    report = _freeze(db_session, accepted, target, candidates)

    assert report["order"] == [r.run_id for r in candidates]
    assert [_req(db_session, r, item).net_required_qty for r in candidates] == pytest.approx([0, 15])
    assert [_purchase(db_session, r, item) for r in candidates] == pytest.approx([0, 13])
    assert [(r.status, r.active_freeze_version) for r in parents] == parent_state
    assert [line.locked_by_run_id for line in lines] == [None, None]
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id


def test_produced_subassembly_stock_is_consumed_once_across_candidates(db_session):
    root = _item(db_session, "ASSEMBLY", method="Производство")
    sub = _item(db_session, "SUB", method="Производство")
    leaf = _item(db_session, "LEAF")
    _bom(db_session, root, sub)
    _bom(db_session, sub, leaf)
    accepted, target, _, _parents, candidates, _lines = _world(
        db_session, [100, 100], root=root, stock={sub: 100}
    )

    _freeze(db_session, accepted, target, candidates)

    assert [_purchase(db_session, run, leaf) for run in candidates] == pytest.approx([0, 100])


def test_freeze_uses_target_stockbin_not_legacy_item_stock_qty(db_session):
    item = _item(db_session, "NO-LEGACY-STOCK")
    accepted, target, _, _parents, candidates, _lines = _world(
        db_session, [10], root=item, stock={item: 4}
    )

    _freeze(db_session, accepted, target, candidates)

    assert float(_req(db_session, candidates[0], item).initial_snapshot_stock) == pytest.approx(4)
    assert _purchase(db_session, candidates[0], item) == pytest.approx(6)


def test_freeze_rows_are_target_scoped_and_published_parent_is_immutable(db_session):
    item = _item(db_session, "ISOLATED")
    accepted, target, _, parents, candidates, _lines = _world(
        db_session, [8], root=item
    )
    parent = parents[0]
    before = (parent.status, parent.fixed_at, parent.active_freeze_version)

    _freeze(db_session, accepted, target, candidates)

    assert (parent.status, parent.fixed_at, parent.active_freeze_version) == before
    assert db_session.query(models.MrpRequirement).filter_by(run_id=parent.run_id).count() == 0
    assert db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=accepted.id
    ).count() == 0
    purchases = db_session.query(models.PlannedPurchase).filter_by(
        ledger_generation_id=target.id, run_id=candidates[0].run_id
    ).all()
    assert purchases and sum(float(row.qty) for row in purchases) == pytest.approx(8)


def test_candidate_freeze_rejects_retry_instead_of_partially_reusing_derived_rows(db_session):
    item = _item(db_session, "RETRY")
    accepted, target, _, _parents, candidates, _lines = _world(
        db_session, [10], root=item
    )
    _freeze(db_session, accepted, target, candidates)
    counts = (
        db_session.query(models.MrpRequirement).count(),
        db_session.query(models.ReservationEntry).count(),
    )

    with pytest.raises(LedgerPoolUnavailable, match="already has derived requirements"):
        _freeze(db_session, accepted, target, candidates)

    assert (
        db_session.query(models.MrpRequirement).count(),
        db_session.query(models.ReservationEntry).count(),
    ) == counts


def test_candidate_freeze_rejects_partial_manifest(db_session):
    item = _item(db_session, "CLOSED-SET")
    accepted, target, _, _parents, candidates, _lines = _world(
        db_session, [2, 3], root=item
    )
    with pytest.raises(LedgerPoolUnavailable, match="ids differ from sealed"):
        freeze_candidate_snapshots(
            db_session,
            parent_generation_id=accepted.id,
            target_generation_id=target.id,
            candidate_run_ids=[candidates[0].run_id],
        )
    assert db_session.query(models.MrpRequirement).count() == 0


def test_candidate_freeze_rejects_stale_parent_pointer(db_session):
    item = _item(db_session, "STALE")
    accepted, target, _, _parents, candidates, _lines = _world(
        db_session, [2], root=item
    )
    db_session.get(models.PlanningTruthState, 1).current_generation_id = target.id
    with pytest.raises(LedgerPoolUnavailable, match="current accepted"):
        _freeze(db_session, accepted, target, candidates)
    assert db_session.query(models.MrpRequirement).count() == 0


def test_retired_destructive_refreeze_entrypoint_fails_closed(db_session):
    with pytest.raises(LedgerPoolUnavailable, match="retired"):
        refreeze_active_snapshots(db_session)


def test_pool_key_normalises_to_default():
    key = pool_key_for(123, characteristic_ref="legacy", organization_ref="legacy")
    assert (
        key.item_id,
        key.characteristic_ref,
        key.organization_ref,
        key.planning_stock_pool,
    ) == (123, "", "", "default")


def test_build_shared_pools_requires_explicit_ledger_generation(db_session):
    with pytest.raises(TypeError):
        build_shared_pools(db_session, [])


def test_build_shared_pools_ignores_ignored_and_unselected_warehouses(db_session):
    item = _item(db_session, "SCOPE-OK")
    target = _freeze_candidate_generation(db_session, suffix="non-block")

    db_session.add_all([
        models.StockWarehouse(warehouse_ref1c="WH-SELECTED", warehouse_name="Selected", is_selected=True),
        models.StockWarehouse(warehouse_ref1c="WH-UNSELECTED", warehouse_name="Unselected", is_selected=False),
        models.StockWarehouse(warehouse_ref1c="WH-IGNORED", warehouse_name="Ignored", is_selected=True),
    ])
    db_session.add(models.IgnoredWarehouse(warehouse_ref1c="WH-IGNORED", warehouse_name="Ignored"))
    db_session.add_all([
        models.StockBin(
            ledger_generation_id=target.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="WH-SELECTED",
            on_hand=10,
        ),
        models.StockBin(
            ledger_generation_id=target.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="ORG-UNSELECTED",
            warehouse_ref1c="WH-UNSELECTED",
            on_hand=7,
        ),
        models.StockBin(
            ledger_generation_id=target.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="ORG-IGNORED",
            warehouse_ref1c="WH-IGNORED",
            on_hand=5,
        ),
    ])
    db_session.flush()

    pools = build_shared_pools(
        db_session,
        [],
        ledger_generation_id=target.id,
        relevant_item_ids=[item.item_id],
    )

    assert pools.stock[item.item_id] == pytest.approx(10)


def test_build_shared_pools_rejects_foreign_org_in_selected_warehouse(db_session):
    item = _item(db_session, "SCOPE-BLOCK")
    target = _freeze_candidate_generation(db_session, suffix="block")
    db_session.add(
        models.StockWarehouse(
            warehouse_ref1c="WH-SELECTED", warehouse_name="Selected", is_selected=True
        )
    )
    db_session.add(
        models.StockBin(
            ledger_generation_id=target.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="ORG-FOREIGN",
            warehouse_ref1c="WH-SELECTED",
            on_hand=10,
        )
    )
    db_session.flush()

    with pytest.raises(LedgerPoolUnavailable, match="characteristic/organization physical pools"):
        build_shared_pools(
            db_session,
            [],
            ledger_generation_id=target.id,
            relevant_item_ids=[item.item_id],
        )
