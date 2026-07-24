"""Service-level contract for neutral purchase-control materialization."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import count

import pytest

from app import models
from app.services import planning_truth
from app.services.purchase_control_materialization import (
    PurchaseControlMaterializationError,
    materialize_rows,
)
from app.services.purchase_control_snapshot import build_candidate_snapshot


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "planning_snapshots": True,
    "purchase_control_journal": True,
}


_fixture_seq = count(1)


def _accepted_generation(db) -> tuple[models.LedgerGeneration, models.PlanningReadSnapshot]:
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    idx = next(_fixture_seq)
    physical = models.PhysicalImportBatch(
        batch_key=f"pcm-generation-batch-{idx}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"pcm-generation-{idx}",
        status="building",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="tests/pcm",
    )
    db.add(generation)
    db.flush()

    item = models.Item(
        item_code=f"PUR-MAT-{idx}",
        item_name="Материал PCM",
        item_ref1c="item-ref-pcm",
        supplier_ref1c=f"SUP-PCM-{idx}",
        unit="шт",
    )
    supplier = models.Supplier(
        supplier_ref1c=f"SUP-PCM-{idx}",
        supplier_name="Поставщик PCM",
    )
    db.add_all([item, supplier])
    db.flush()

    return generation, item, supplier


def _add_buy_run(
    db,
    *,
    generation,
    item,
    period_from: date,
    period_to: date,
    required_qty: Decimal,
    realized_qty: Decimal,
    covered_incoming: Decimal,
    uncovered: Decimal,
):
    plan = models.ProductionPlanHeader(
        name=f"buy-run-{period_from.isoformat()}",
        period_from=period_from,
        period_to=period_to,
        status="fixed",
    )
    db.add(plan)
    db.flush()

    planning_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=period_from,
        period_to=period_to,
        ledger_generation_id=generation.id,
        config_snapshot={"plan": "pcm"},
    )
    db.add(planning_run)
    db.flush()

    requirement = models.MrpRequirement(
        run_id=planning_run.run_id,
        item_id=item.item_id,
        total_required_qty=required_qty,
        net_required_qty=required_qty,
        covered_qty=Decimal("0"),
        remaining_qty=required_qty,
        period_from=period_from,
        period_to=period_to,
        bom_level=1,
    )
    db.add(requirement)
    db.flush()

    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="main",
        run_id=planning_run.run_id,
        freeze_version=0,
        requirement_id=requirement.id,
        priority_period_from=period_from,
        priority_period_to=period_to,
        realization_mode="buy",
        reserved_qty=required_qty,
        realized_qty=realized_qty,
        covered_incoming_supplier_qty=covered_incoming,
        covered_incoming_wip_qty=Decimal("0"),
        uncovered_qty=uncovered,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()

    if covered_incoming > Decimal("0"):
        db.add(
            models.ReservationCoverage(
                reservation_id=reservation.id,
                source_kind="supplier_order",
                source_ref="supplier-1",
                source_line_ref="10",
                pin_kind="incoming",
                alloc_qty=covered_incoming,
                covered_qty=covered_incoming,
                realized_qty=covered_incoming,
                evaporated_qty=Decimal("0"),
            )
        )
    db.flush()

    return reservation, planning_run


def _accept_generation_snapshot(db, generation: models.LedgerGeneration, snapshot: models.PlanningReadSnapshot):
    accepted_at = generation.cutoff + timedelta(hours=1)
    generation.status = "accepted"
    generation.accepted_at = accepted_at
    generation.capabilities = dict(CAPABILITIES)
    snapshot.truth_status = "accepted"
    snapshot.reason = None
    snapshot.published_at = accepted_at
    planning_truth.publish_generation(db, generation)
    db.flush()
    return accepted_at, snapshot.id


def _stale_generation_fixture(db):
    old_generation, item, _supplier = _accepted_generation(db)
    old_reservation, _old_run = _add_buy_run(
        db,
        generation=old_generation,
        item=item,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        required_qty=Decimal("10"),
        realized_qty=Decimal("2"),
        covered_incoming=Decimal("0"),
        uncovered=Decimal("8"),
    )
    old_snapshot = build_candidate_snapshot(db, old_generation.id)
    _accept_generation_snapshot(db, old_generation, old_snapshot)

    current_generation, _, _ = _accepted_generation(db)
    current_generation.status = "accepted"
    current_generation.accepted_at = datetime(2026, 7, 24, 13, tzinfo=timezone.utc)
    current_generation.capabilities = dict(CAPABILITIES)
    payload = deepcopy(old_snapshot.payload)
    payload["meta"] = dict(payload.get("meta") or {})
    payload["meta"].update(
        {
            "ledger_generation": current_generation.id,
            "ledger_generation_id": current_generation.id,
            "truth_status": "accepted",
            "cutoff": current_generation.cutoff.isoformat(),
        }
    )
    stale_snapshot = models.PlanningReadSnapshot(
        consumer="purchase_control_journal",
        snapshot_key="journal:v1",
        ledger_generation_id=current_generation.id,
        cutoff=current_generation.cutoff,
        truth_status="accepted",
        reason="stale-row-fixture",
        payload=payload,
        published_at=current_generation.accepted_at,
    )
    db.add(stale_snapshot)
    db.add(current_generation)
    db.flush()
    planning_truth.publish_generation(db, current_generation)
    db.flush()

    return current_generation, stale_snapshot, old_reservation


def _build_multi_run_snapshot(db) -> tuple[models.LedgerGeneration, models.PlanningReadSnapshot]:
    generation, item, _supplier = _accepted_generation(db)
    _add_buy_run(
        db,
        generation=generation,
        item=item,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        required_qty=Decimal("10"),
        realized_qty=Decimal("3"),
        covered_incoming=Decimal("0"),
        uncovered=Decimal("7"),
    )
    _add_buy_run(
        db,
        generation=generation,
        item=item,
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        required_qty=Decimal("12"),
        realized_qty=Decimal("4"),
        covered_incoming=Decimal("1"),
        uncovered=Decimal("7"),
    )

    snapshot = build_candidate_snapshot(db, generation.id)
    _accept_generation_snapshot(db, generation, snapshot)
    assert len(snapshot.payload.get("rows", [])) == 1
    return generation, snapshot


def _snapshot_first_row(snapshot: models.PlanningReadSnapshot) -> dict:
    rows = snapshot.payload.get("rows")
    assert isinstance(rows, list) and rows, "snapshot rows are required for materialize test"
    return dict(rows[0])


def _materializer_with_records(db, groups, request_payload, batch_id, dry_run):
    _ = (db, request_payload, batch_id, dry_run)
    if len(groups) != 1:
        raise AssertionError("expected one supplier group")
    allocations = []
    for index, line in enumerate(groups[0].lines):
        allocations.append({
            "reservation_id": int(line.reservation_id),
            "supplier_order_ref": "PO-TEST",
            "supplier_order_line_no": str(index + 1),
            "allocated_qty": float(line.qty),
            "line_token": 1000 + index,
            "line_hash": f"token-{index + 1}",
        })
    return len(allocations), allocations, {"writer": "ok"}


def _recording_materializer(records: list[str], _db, _groups, _request_payload, _batch_id, _dry_run):
    records.append("called")
    return _materializer_with_records(_db, _groups, _request_payload, _batch_id, _dry_run)


def test_materialize_rows_rejects_stale_snapshot_id(db_session):
    generation, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)

    with pytest.raises(PurchaseControlMaterializationError, match="requested snapshot_id does not match"):
        materialize_rows(
            db_session,
            snapshot_id=snapshot.id + 1,
            row_keys=[row["row_key"]],
            dry_run=True,
        )


def test_materialize_rows_dry_run_writes_nothing(db_session):
    generation, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)

    assert db_session.query(models.PurchaseExportBatch).count() == 0

    preview = materialize_rows(
        db_session,
        snapshot_id=snapshot.id,
        row_keys=[row["row_key"]],
        dry_run=True,
    )

    assert preview["dry_run"] is True
    assert preview["snapshot_id"] == snapshot.id
    assert preview["rows_total"] == 1
    assert db_session.query(models.PurchaseExportBatch).count() == 0
    assert db_session.query(models.PurchaseExportObligationAllocation).count() == 0


def test_materialize_rows_rejects_stale_reservation_generation(db_session):
    current_generation, snapshot, stale_reservation = _stale_generation_fixture(db_session)
    row = _snapshot_first_row(snapshot)

    with pytest.raises(PurchaseControlMaterializationError, match="stale generation"):
        materialize_rows(
            db_session,
            snapshot_id=snapshot.id,
            row_keys=[row["row_key"]],
            dry_run=False,
            materializer=lambda *_args, **_kwargs: _materializer_with_records(*_args, **_kwargs),
        )


def test_materialize_rows_idempotent_retry_and_durable_lineage(db_session, monkeypatch):
    generation, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)
    calls: list[str] = []

    def materializer(db, groups, request_payload, batch_id, dry_run):
        _ = (db, request_payload, batch_id, dry_run)
        calls.append("invoked")
        return _materializer_with_records(db, groups, request_payload, batch_id, dry_run)

    first = materialize_rows(
        db_session,
        snapshot_id=snapshot.id,
        row_keys=[row["row_key"]],
        dry_run=False,
        materializer=materializer,
    )

    assert first["status"] == "completed"
    assert first["batch_id"] > 0
    assert first["idempotency_key"]
    assert len(calls) == 1

    allocations = db_session.query(models.PurchaseExportObligationAllocation).all()
    assert len(allocations) == len(row["reservation_ids"])
    assert {alloc.reservation_id for alloc in allocations} == set(row["reservation_ids"])
    assert {alloc.line_hash for alloc in allocations} == {"token-1", "token-2"}
    assert all(alloc.line_token is not None for alloc in allocations)

    second = materialize_rows(
        db_session,
        snapshot_id=snapshot.id,
        row_keys=[row["row_key"]],
        dry_run=False,
        materializer=materializer,
    )

    assert second["batch_id"] == first["batch_id"]
    assert second["idempotency_key"] == first["idempotency_key"]
    assert second["status"] == "completed"
    assert len(calls) == 1
