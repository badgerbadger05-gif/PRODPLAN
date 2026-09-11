from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.mrp_result_projection import build_mrp_result_current_payload


def _accepted_generation(db, key: str):
    cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"{key}:physical",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={
            "execution_allocations": True,
            "planning_snapshots": True,
        },
        physical_import_batch=physical,
        algorithm_version="r10-lineage-test",
    )
    db.add(generation)
    db.flush()
    return generation


def _set_current_truth(db, generation):
    db.add(
        models.PlanningTruthState(
            id=1,
            current_generation_id=generation.id,
        )
    )
    db.flush()


def _physical_sibling(db, parent, key: str):
    cutoff = datetime(2026, 9, 11, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"{key}:physical",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={},
    )
    sibling = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(parent.id),
        },
        capabilities=dict(parent.capabilities or {}),
        physical_import_batch=physical,
        algorithm_version="r10-lineage-test",
    )
    db.add(sibling)
    db.flush()
    return sibling


def _fixed_run(db, generation):
    item = models.Item(
        item_code=f"R10-LINEAGE-{generation.id}",
        item_name="R10 lineage item",
        unit="шт",
    )
    db.add(item)
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db.add(run)
    db.flush()
    return run, item


def _add_obligation(db, kind, run, item, lineage):
    common = dict(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=Decimal("2"),
        planned_qty=Decimal("2"),
        qty=Decimal("2"),
        need_date=date(2026, 9, 12),
        bucket_date=date(2026, 9, 12),
        ledger_generation_id=lineage,
    )
    if kind == "purchase":
        db.add(
            models.PlannedPurchase(
                **common,
                order_date=date(2026, 9, 10),
                lead_time_days=1,
            )
        )
    elif kind == "order":
        db.add(
            models.PlannedOrder(
                **common,
                start_date=date(2026, 9, 11),
                finish_date=date(2026, 9, 12),
            )
        )
    else:
        db.add(
            models.PlannedRework(
                **common,
                order_date=date(2026, 9, 10),
                lead_time_days=1,
                spec_id=None,
            )
        )
    db.flush()


@pytest.mark.parametrize("kind", ["purchase", "order", "rework"])
@pytest.mark.parametrize("lineage", ["null", "foreign"])
def test_fixed_mrp_payload_rejects_unbound_obligation_lineage(
    db_session, kind, lineage
):
    generation = _accepted_generation(db_session, f"r10-lineage-{kind}-{lineage}")
    run, item = _fixed_run(db_session, generation)
    foreign = _accepted_generation(db_session, f"r10-foreign-{kind}-{lineage}")
    _set_current_truth(db_session, generation)
    _add_obligation(
        db_session,
        kind,
        run,
        item,
        None if lineage == "null" else foreign.id,
    )

    with pytest.raises(ValueError, match="NULL or foreign"):
        build_mrp_result_current_payload(db_session, run.run_id)


def test_fixed_mrp_payload_requires_generation_binding(db_session):
    generation = _accepted_generation(db_session, "r10-lineage-missing-generation")
    run, _item = _fixed_run(db_session, generation)
    run.ledger_generation_id = None
    db_session.flush()

    with pytest.raises(ValueError, match="generation"):
        build_mrp_result_current_payload(db_session, run.run_id)


def test_fixed_mrp_payload_accepts_accepted_generation_lineage(db_session):
    generation = _accepted_generation(db_session, "r10-lineage-valid")
    run, item = _fixed_run(db_session, generation)
    _add_obligation(db_session, "purchase", run, item, generation.id)
    _set_current_truth(db_session, generation)

    payload = build_mrp_result_current_payload(db_session, run.run_id)

    assert payload["run_id"] == run.run_id
    assert payload["row_counts"]["purchase"] == 1
    assert payload["rows"][0]["payload"]["item_id"] == item.item_id


def test_fixed_mrp_payload_rejects_anchor_outside_current_sealed_lineage(db_session):
    base = _accepted_generation(db_session, "r10-lineage-base")
    run_anchor = _physical_sibling(db_session, base, "r10-lineage-anchor")
    current_generation = _physical_sibling(db_session, base, "r10-lineage-current")
    run, item = _fixed_run(db_session, run_anchor)
    _add_obligation(db_session, "purchase", run, item, run_anchor.id)
    _set_current_truth(db_session, current_generation)

    with pytest.raises(ValueError, match="outside the sealed lineage"):
        build_mrp_result_current_payload(db_session, run.run_id)
