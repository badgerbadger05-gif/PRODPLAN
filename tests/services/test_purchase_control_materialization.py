"""Service-level contract for neutral purchase-control materialization."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import count

import pytest

from app import models
from app.services import planning_truth
from app.services import purchase_control_materialization as pcm
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


class _FakePurchaseControlODataClient:
    def __init__(self, *, existing_docs: list[dict] | None = None, fail: bool = False):
        self.existing_docs = list(existing_docs or [])
        self.fail = fail
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def get_all(self, entity: str, *, filter_query: str | None = None, **_kwargs):
        self.gets.append(filter_query or "")
        if self.fail:
            raise RuntimeError("simulated get_all failure")
        return list(self.existing_docs)

    def post(self, entity: str, payload: dict) -> dict:
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated post failure")
        return {
            "Ref_Key": f"po-ref-{len(self.posts)}",
            "Контрагент_Key": payload.get("Контрагент_Key"),
            "Запасы": payload.get("Запасы") or [],
        }


def _as_expected_alloc_map(row: dict) -> dict[int, float]:
    allocations: dict[int, float] = {}
    for slice_row in row.get("slices", []) or []:
        rid = slice_row.get("reservation_id")
        qty = round(float(slice_row.get("to_order_qty") or 0.0), 6)
        if rid is None or qty <= 0:
            continue
        allocations[int(rid)] = round(allocations.get(int(rid), 0.0) + qty, 6)
    return allocations


def _materialization_row_from_slice(
    base_row: dict,
    slice_row: dict,
    *,
    suffix: str,
    supplier_id: int | None = None,
) -> dict:
    reservation_id = int(slice_row.get("reservation_id"))
    required_qty = round(float(slice_row.get("required_qty") or 0.0), 3)
    realized_qty = round(float(slice_row.get("realized_qty") or 0.0), 3)
    open_order_covered_qty = round(
        float(slice_row.get("open_order_covered_qty") or 0.0),
        3,
    )
    to_order_qty = round(float(slice_row.get("to_order_qty") or 0.0), 3)
    run_id = int(slice_row.get("run_id"))

    row = dict(base_row)
    row["row_key"] = f"{base_row['row_key']}:{suffix}"
    row["supplier_id"] = int(supplier_id) if supplier_id is not None else int(base_row["supplier_id"])
    row["reservation_ids"] = [reservation_id]
    row["requirement_ids"] = [int(slice_row.get("requirement_id"))]
    row["run_id"] = run_id
    row["run_ids"] = [run_id]
    row["required_qty"] = required_qty
    row["realized_qty"] = realized_qty
    row["open_order_covered_qty"] = open_order_covered_qty
    row["to_order_qty"] = to_order_qty
    row["quantity"] = required_qty
    row["received_qty"] = realized_qty
    row["remaining_qty"] = to_order_qty
    row["to_order_pct"] = 0.0 if required_qty == 0 else round(
        to_order_qty / required_qty * 100.0,
        6,
    )
    row["open_order_covered_pct"] = 0.0 if required_qty == 0 else round(
        open_order_covered_qty / required_qty * 100.0,
        6,
    )
    row["slices"] = [dict(slice_row)]
    row["horizon_buckets"] = [dict(slice_row)]
    row["plan_period_from"] = str(slice_row.get("plan_period_from") or base_row.get("plan_period_from"))
    row["plan_period_to"] = str(slice_row.get("plan_period_to") or base_row.get("plan_period_to"))

    return row


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


def test_materialize_rows_aggregates_duplicate_reservation_ids_in_expected(db_session):
    _generation, snapshot = _build_multi_run_snapshot(db_session)
    rows = snapshot.payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise AssertionError("snapshot row set is malformed")
    row = rows[0]
    slices = row.get("slices")
    if not isinstance(slices, list) or not slices:
        raise AssertionError("snapshot row has no allocation slices")

    reservation_id = slices[0].get("reservation_id")
    if reservation_id is None:
        raise AssertionError("snapshot slice is missing reservation_id")

    original_qty = round(float(slices[0].get("to_order_qty") or 0.0), 6)
    first_qty = round(original_qty / 2, 6)
    second_qty = round(original_qty - first_qty, 6)
    slices[0]["to_order_qty"] = first_qty
    duplicated = dict(slices[0])
    duplicated["to_order_qty"] = second_qty
    duplicated["reservation_id"] = reservation_id
    slices.append(duplicated)
    row["slices"] = slices
    expected_line_count = sum(
        1
        for slice_row in slices
        if float(slice_row.get("to_order_qty") or 0.0) > 0 and slice_row.get("reservation_id") is not None
    )
    db_session.flush()

    row_key = row["row_key"]
    expected_allocs = _as_expected_alloc_map(row)

    result = materialize_rows(
        db_session,
        snapshot_id=snapshot.id,
        row_keys=[row_key],
        dry_run=False,
        materializer=lambda *_args, **_kwargs: _materializer_with_records(*_args, **_kwargs),
    )

    assert result["status"] == "completed"
    allocations = db_session.query(models.PurchaseExportObligationAllocation).all()
    assert len(allocations) == expected_line_count
    by_reservation: dict[int, float] = {}
    for alloc in allocations:
        rid = int(alloc.reservation_id)
        by_reservation[rid] = round(by_reservation.get(rid, 0.0) + float(alloc.allocated_qty), 6)
    assert by_reservation == expected_allocs


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


def test_materialize_rows_default_writer_posts_and_verifies_payload(db_session, monkeypatch):
    _, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)
    expected_allocs = _as_expected_alloc_map(row)

    client = _FakePurchaseControlODataClient()

    monkeypatch.setattr(pcm, "_load_odata_config", lambda: {"base_url": "http://mtzdock/unf_demo/odata/standard.odata", "purchase_destination_warehouse_ref1c": "00000000-0000-0000-0000-000000000001"})
    monkeypatch.setattr(pcm, "OData1CClient", lambda **_: client)

    result = materialize_rows(
        db_session,
        snapshot_id=snapshot.id,
        row_keys=[row["row_key"]],
        dry_run=False,
    )

    assert result["status"] == "completed"
    assert len(client.posts) == 1
    assert client.gets, "default writer should query marker recovery"

    allocations = db_session.query(models.PurchaseExportObligationAllocation).all()
    assert len(allocations) == len(expected_allocs)
    assert {alloc.reservation_id: round(float(alloc.allocated_qty), 6) for alloc in allocations} == expected_allocs
    assert all(alloc.line_hash is not None for alloc in allocations)
    assert all(alloc.line_token is not None for alloc in allocations)


def test_materialize_rows_default_writer_recovers_existing_order_without_post(db_session, monkeypatch):
    monkeypatch.setattr(pcm, "_load_odata_config", lambda: {"base_url": "http://mtzdock/unf_demo/odata/standard.odata", "purchase_destination_warehouse_ref1c": "00000000-0000-0000-0000-000000000001"})
    generation, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)
    accepted_snapshot = pcm.purchase_control_snapshot.read_snapshot(db_session)
    groups, selected_rows, ledger_generation_id = pcm._load_groups_and_lineages(
        db_session, accepted_snapshot, [row["row_key"]]
    )
    request = pcm._build_request_payload(
        snapshot=accepted_snapshot,
        requested_keys=[row["row_key"]],
        selected_rows=selected_rows,
        groups=groups,
        ledger_generation_id=ledger_generation_id,
    )
    assert ledger_generation_id == generation.id

    for group in groups:
        pcm._stamp_group_lines(group)
    existing_doc = {
        "Ref_Key": "po-ref-existing",
        "Контрагент_Key": groups[0].supplier_ref1c,
        "Запасы": pcm._order_lines_payload("po-ref-existing", groups[0]),
    }

    client = _FakePurchaseControlODataClient(existing_docs=[existing_doc])
    monkeypatch.setattr(pcm, "OData1CClient", lambda **_: client)

    created, allocations, _writer = pcm._materialize_purchase_control_orders_to_1c(
        db_session,
        groups,
        request,
        1,
        False,
    )

    assert created == 0
    assert allocations, "existing marker should be reused"
    assert not client.posts
    assert client.gets


def test_materialize_rows_default_writer_rejects_line_payload_mismatch(db_session, monkeypatch):
    monkeypatch.setattr(pcm, "_load_odata_config", lambda: {"base_url": "http://mtzdock/unf_demo/odata/standard.odata", "purchase_destination_warehouse_ref1c": "00000000-0000-0000-0000-000000000001"})
    _, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)
    accepted_snapshot = pcm.purchase_control_snapshot.read_snapshot(db_session)
    groups, selected_rows, ledger_generation_id = pcm._load_groups_and_lineages(
        db_session, accepted_snapshot, [row["row_key"]]
    )
    request = pcm._build_request_payload(
        snapshot=accepted_snapshot,
        requested_keys=[row["row_key"]],
        selected_rows=selected_rows,
        groups=groups,
        ledger_generation_id=ledger_generation_id,
    )
    for group in groups:
        pcm._stamp_group_lines(group)

    lines = pcm._order_lines_payload("po-ref-existing", groups[0])
    lines[0]["Количество"] += 1.0
    existing_doc = {
        "Ref_Key": "po-ref-existing",
        "Контрагент_Key": groups[0].supplier_ref1c,
        "Запасы": lines,
    }

    client = _FakePurchaseControlODataClient(existing_docs=[existing_doc])
    monkeypatch.setattr(pcm, "OData1CClient", lambda **_: client)

    with pytest.raises(RuntimeError, match="line payload mismatch"):
        pcm._materialize_purchase_control_orders_to_1c(
            db_session,
            groups,
            request,
            1,
            False,
        )


def test_materialize_rows_default_writer_rejects_duplicate_marker_search_result(db_session, monkeypatch):
    monkeypatch.setattr(pcm, "_load_odata_config", lambda: {"base_url": "http://mtzdock/unf_demo/odata/standard.odata", "purchase_destination_warehouse_ref1c": "00000000-0000-0000-0000-000000000001"})
    _, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)
    accepted_snapshot = pcm.purchase_control_snapshot.read_snapshot(db_session)
    groups, selected_rows, ledger_generation_id = pcm._load_groups_and_lineages(
        db_session, accepted_snapshot, [row["row_key"]]
    )
    request = pcm._build_request_payload(
        snapshot=accepted_snapshot,
        requested_keys=[row["row_key"]],
        selected_rows=selected_rows,
        groups=groups,
        ledger_generation_id=ledger_generation_id,
    )
    for group in groups:
        pcm._stamp_group_lines(group)

    duplicate = {
        "Ref_Key": "po-ref-dup-1",
        "Контрагент_Key": groups[0].supplier_ref1c,
        "Запасы": pcm._order_lines_payload("po-ref-dup-1", groups[0]),
    }
    client = _FakePurchaseControlODataClient(existing_docs=[duplicate, duplicate])
    monkeypatch.setattr(pcm, "OData1CClient", lambda **_: client)

    with pytest.raises(RuntimeError, match="несколько документов"):
        pcm._materialize_purchase_control_orders_to_1c(
            db_session,
            groups,
            request,
            1,
            False,
        )


def test_materialize_rows_partial_batch_recovery_per_group(db_session):
    _, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)
    slices = row.get("slices")
    if not isinstance(slices, list) or len(slices) < 2:
        raise AssertionError("snapshot row must contain at least two slices")

    second_supplier = models.Supplier(
        supplier_ref1c="SUP-PCM-OTHER",
        supplier_name="Второй поставщик PCM",
    )
    db_session.add(second_supplier)
    db_session.flush()

    snapshot_rows: list[dict] = [
        _materialization_row_from_slice(
            row,
            slices[0],
            suffix="r1",
            supplier_id=int(row["supplier_id"]),
        ),
        _materialization_row_from_slice(
            row,
            slices[1],
            suffix="r2",
            supplier_id=int(second_supplier.supplier_id),
        ),
    ]
    snapshot.payload = {**snapshot.payload, "rows": snapshot_rows}
    db_session.flush()

    fail_calls: dict[int, int] = {}
    first_supplier_id = int(snapshot_rows[0]["supplier_id"])
    failing_supplier_id = int(second_supplier.supplier_id)

    def materializer(db, groups, request_payload, batch_id, dry_run):
        _ = (db, request_payload, batch_id, dry_run)
        group_supplier_id = int(groups[0].supplier_id)
        fail_calls[group_supplier_id] = fail_calls.get(group_supplier_id, 0) + 1
        if group_supplier_id == failing_supplier_id and fail_calls[group_supplier_id] == 1:
            raise RuntimeError("simulated group failure")
        return _materializer_with_records(db, groups, request_payload, batch_id, dry_run)

    with pytest.raises(RuntimeError, match="simulated group failure"):
        materialize_rows(
            db_session,
            snapshot_id=snapshot.id,
            row_keys=[snapshot_rows[0]["row_key"], snapshot_rows[1]["row_key"]],
            dry_run=False,
            materializer=materializer,
        )

    first_batch = (
        db_session.query(models.PurchaseExportBatch)
        .order_by(models.PurchaseExportBatch.id.desc())
        .first()
    )
    assert first_batch is not None
    assert first_batch.status == "failed"
    assert len(db_session.query(models.PurchaseExportObligationAllocation).filter_by(batch_id=first_batch.id).all()) == 1

    second = materialize_rows(
        db_session,
        snapshot_id=snapshot.id,
        row_keys=[snapshot_rows[0]["row_key"], snapshot_rows[1]["row_key"]],
        dry_run=False,
        materializer=materializer,
    )

    assert second["status"] == "completed"
    assert second["batch_id"] == int(first_batch.id)

    allocations = db_session.query(models.PurchaseExportObligationAllocation).all()
    assert len(allocations) == 2
    assert fail_calls.get(first_supplier_id, 0) == 1
    assert fail_calls.get(failing_supplier_id, 0) == 2


def test_materialize_rows_rejects_overlap_with_successful_sync_link(db_session):
    _generation, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)
    slices = row.get("slices")
    if not isinstance(slices, list) or len(slices) < 2:
        raise AssertionError("snapshot row must contain at least two slices")
    rows = [
        _materialization_row_from_slice(
            row,
            slices[0],
            suffix="a",
            supplier_id=int(row["supplier_id"]),
        ),
        _materialization_row_from_slice(
            row,
            slices[1],
            suffix="b",
            supplier_id=int(row["supplier_id"]),
        ),
    ]
    snapshot.payload = {**snapshot.payload, "rows": rows}
    db_session.flush()

    first = materialize_rows(
        db_session,
        snapshot_id=snapshot.id,
        row_keys=[rows[0]["row_key"]],
        dry_run=False,
        materializer=lambda *_args, **_kwargs: _materializer_with_records(
            *_args,
            **_kwargs,
        ),
    )
    assert first["status"] == "completed"

    with pytest.raises(PurchaseControlMaterializationError, match="already materialized BUY reservations"):
        materialize_rows(
            db_session,
            snapshot_id=snapshot.id,
            row_keys=[rows[0]["row_key"], rows[1]["row_key"]],
            dry_run=False,
            materializer=lambda *_args, **_kwargs: _materializer_with_records(*_args, **_kwargs),
        )

    assert db_session.query(models.PurchaseExportObligationAllocation).count() == 1


def test_materialize_rows_recovers_after_external_post_before_local_persistence(
    db_session,
    monkeypatch,
):
    _, snapshot = _build_multi_run_snapshot(db_session)
    row = _snapshot_first_row(snapshot)
    posted_groups: set[str] = set()
    post_count = 0

    def recovering_materializer(db, groups, request_payload, batch_id, dry_run):
        nonlocal post_count
        group_key = pcm._one_c_line_payload_token_group_hash(groups[0])
        if group_key not in posted_groups:
            posted_groups.add(group_key)
            post_count += 1
        return _materializer_with_records(
            db,
            groups,
            request_payload,
            batch_id,
            dry_run,
        )

    original_builder = pcm._build_allocation_records
    fail_once = True

    def failing_builder(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("simulated local persistence failure")
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(pcm, "_build_allocation_records", failing_builder)
    with pytest.raises(RuntimeError, match="simulated local persistence failure"):
        materialize_rows(
            db_session,
            snapshot_id=snapshot.id,
            row_keys=[row["row_key"]],
            dry_run=False,
            materializer=recovering_materializer,
        )

    failed_batch = db_session.query(models.PurchaseExportBatch).one()
    assert failed_batch.status == "failed"
    assert post_count == 1
    assert db_session.query(models.PurchaseExportObligationAllocation).count() == 0

    result = materialize_rows(
        db_session,
        snapshot_id=snapshot.id,
        row_keys=[row["row_key"]],
        dry_run=False,
        materializer=recovering_materializer,
    )

    assert result["status"] == "completed"
    assert result["batch_id"] == int(failed_batch.id)
    assert post_count == 1
    assert db_session.query(models.PurchaseExportObligationAllocation).count() == len(
        row["reservation_ids"]
    )
