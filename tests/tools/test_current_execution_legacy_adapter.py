"""Row-carrier and fixed-run selection contracts of the R10 legacy adapter.

The canonical current publisher is patched here on purpose: these tests are
about *which legacy evidence the adapter hands over*, which is exactly where a
real stand silently published two empty scopes.  The publisher's own semantics
are covered by its own tests and by the PostgreSQL rehearsal.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from app import models
from app.services.item_ledger import current_execution as current_execution_module
from tools.current_execution_legacy_adapter import (
    LegacyEvidenceConflict,
    publish_current_obligation_views_from_snapshots,
)


_STAMP = datetime(2026, 9, 11, tzinfo=timezone.utc)


_LEGACY_READ_TABLES = (
    "planning_read_root_member", "planning_read_row", "planning_read_snapshot",
)


@pytest.fixture(autouse=True)
def _drop_legacy_read_tables(db_session):
    """Keep the pre-drop tables out of every other test's schema.

    SQLite commits DDL immediately, and the shared in-memory engine only drops
    ORM metadata between tests, so these deliberately non-ORM tables would
    otherwise leak into unrelated suites.
    """

    yield
    db_session.rollback()
    for name in _LEGACY_READ_TABLES:
        db_session.execute(text(f"DROP TABLE IF EXISTS {name}"))
    db_session.commit()


def _create_legacy_read_tables(db_session) -> None:
    for name in _LEGACY_READ_TABLES:
        db_session.execute(text(f"DROP TABLE IF EXISTS {name}"))
    db_session.execute(text(
        "CREATE TABLE planning_read_snapshot ("
        "id INTEGER PRIMARY KEY, consumer TEXT NOT NULL, snapshot_key TEXT NOT NULL, "
        "ledger_generation_id INTEGER NOT NULL, truth_status TEXT NOT NULL, payload TEXT NOT NULL)"
    ))
    db_session.execute(text(
        "CREATE TABLE planning_read_row ("
        "id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, row_key TEXT NOT NULL, "
        "row_kind TEXT NOT NULL DEFAULT '', item_id INTEGER, sort_key TEXT, payload TEXT NOT NULL)"
    ))
    db_session.execute(text(
        "CREATE TABLE planning_read_root_member ("
        "id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, row_id INTEGER NOT NULL, "
        "root_key TEXT NOT NULL, root_item_id INTEGER, payload TEXT NOT NULL DEFAULT '{}')"
    ))


def _snapshot(db_session, snapshot_id, consumer, key, generation_id, payload) -> int:
    db_session.execute(text(
        "INSERT INTO planning_read_snapshot "
        "(id, consumer, snapshot_key, ledger_generation_id, truth_status, payload) "
        "VALUES (:id, :consumer, :key, :generation_id, 'accepted', :payload)"
    ), {
        "id": int(snapshot_id), "consumer": consumer, "key": key,
        "generation_id": int(generation_id),
        "payload": json.dumps(payload, ensure_ascii=False),
    })
    return int(snapshot_id)


def _read_row(db_session, row_id, snapshot_id, *, row_key, payload, row_kind="", sort_key="") -> None:
    db_session.execute(text(
        "INSERT INTO planning_read_row (id, snapshot_id, row_key, row_kind, sort_key, payload) "
        "VALUES (:id, :snapshot_id, :row_key, :row_kind, :sort_key, :payload)"
    ), {
        "id": int(row_id), "snapshot_id": int(snapshot_id), "row_key": row_key,
        "row_kind": row_kind, "sort_key": sort_key,
        "payload": json.dumps(payload, ensure_ascii=False),
    })


def _generation(db_session, key: str) -> models.LedgerGeneration:
    batch = models.PhysicalImportBatch(
        batch_key=f"{key}-batch", status="completed", cutoff=_STAMP,
        source_watermarks={}, completed_at=_STAMP, source_complete=True,
    )
    db_session.add(batch)
    db_session.flush()
    generation = models.LedgerGeneration(
        generation_key=key, status="accepted", cutoff=_STAMP, source_watermarks={},
        capabilities={}, physical_import_batch_id=batch.id,
        algorithm_version="legacy-adapter-test", accepted_at=_STAMP,
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def _capture_publisher(monkeypatch) -> dict:
    captured: dict = {}

    def _publisher(session, generation_id, **kwargs):
        captured["generation_id"] = int(generation_id)
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(
        current_execution_module,
        "publish_current_obligation_views_from_generation",
        _publisher,
    )
    return captured


def _buy_owner(db_session, generation, *, run_id, requirement_id=501) -> None:
    db_session.add(models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=1001,
        run_id=run_id,
        requirement_id=requirement_id,
        priority_period_from=date(2026, 9, 11),
        priority_period_to=date(2026, 9, 12),
        realization_mode="buy",
        lifecycle_status="active",
        owner_kind="current",
        is_current=True,
        current_identity=f"reservation:{requirement_id}:buy",
        replenishment_required_qty=5,
    ))
    db_session.flush()


def test_inline_purchase_rows_are_published_when_the_row_table_is_empty(db_session, monkeypatch):
    """818 real rows lived inline; the row table had none for that snapshot."""

    _create_legacy_read_tables(db_session)
    pointer = _generation(db_session, "adapter-pointer")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=pointer.id))
    _snapshot(db_session, 1, "production_control_journal", "journal:v1", pointer.id, {"meta": {}})
    _read_row(
        db_session, 1, 1,
        row_key="work-item:1001",
        payload={"order_id": 9001, "product_id": 1001, "item_id": 1001, "planned_qty": 2},
    )
    _snapshot(
        db_session, 2, "purchase_control_journal", "journal:v1", pointer.id,
        {
            "meta": {"row_count": 2},
            "rows": [
                {"row_key": "buy:501", "item_id": 1001, "line_status": "to_order"},
                {"row_key": "buy:502", "item_id": 1002, "line_status": "ordered"},
            ],
        },
    )
    db_session.flush()

    captured = _capture_publisher(monkeypatch)
    publish_current_obligation_views_from_snapshots(db_session, pointer.id)

    purchase_rows = captured["purchase_payload"]["rows"]
    assert [row["row_key"] for row in purchase_rows] == ["buy:501", "buy:502"]
    # The production snapshot uses the other carrier in the same database.
    assert [row["order_id"] for row in captured["production_payload"]["rows"]] == [9001]
    assert captured["generation_id"] == pointer.id


def test_row_table_rows_win_and_keep_root_item_ids(db_session, monkeypatch):
    _create_legacy_read_tables(db_session)
    pointer = _generation(db_session, "adapter-rowtable")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=pointer.id))
    _snapshot(db_session, 1, "production_control_journal", "journal:v1", pointer.id, {"meta": {}})
    _read_row(
        db_session, 1, 1,
        row_key="work-item:1001",
        payload={"order_id": 9001, "product_id": 1001, "item_id": 1001},
    )
    db_session.execute(text(
        "INSERT INTO planning_read_root_member (id, snapshot_id, row_id, root_key, root_item_id, payload) "
        "VALUES (1, 1, 1, 'root:1001', 1001, '{}')"
    ))
    _snapshot(
        db_session, 2, "purchase_control_journal", "journal:v1", pointer.id,
        {"meta": {}, "rows": []},
    )
    _read_row(db_session, 2, 2, row_key="buy:501", payload={"row_key": "buy:501", "item_id": 1001})
    db_session.flush()

    captured = _capture_publisher(monkeypatch)
    publish_current_obligation_views_from_snapshots(db_session, pointer.id)

    assert captured["production_payload"]["rows"][0]["root_item_ids"] == [1001]
    assert [row["row_key"] for row in captured["purchase_payload"]["rows"]] == ["buy:501"]


def test_disagreeing_row_carriers_fail_closed(db_session, monkeypatch):
    _create_legacy_read_tables(db_session)
    pointer = _generation(db_session, "adapter-conflict")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=pointer.id))
    _snapshot(db_session, 1, "production_control_journal", "journal:v1", pointer.id, {"meta": {}})
    _snapshot(
        db_session, 2, "purchase_control_journal", "journal:v1", pointer.id,
        {
            "meta": {},
            "rows": [
                {"row_key": "buy:501"},
                {"row_key": "buy:502"},
            ],
        },
    )
    _read_row(db_session, 1, 2, row_key="buy:501", payload={"row_key": "buy:501"})
    db_session.flush()

    _capture_publisher(monkeypatch)
    with pytest.raises(LegacyEvidenceConflict, match="disagrees"):
        publish_current_obligation_views_from_snapshots(db_session, pointer.id)


def test_empty_purchase_evidence_with_live_buy_owners_fails_closed(db_session, monkeypatch):
    _create_legacy_read_tables(db_session)
    pointer = _generation(db_session, "adapter-empty-purchase")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=pointer.id))
    db_session.add(models.PlanningRun(
        run_id=41, source_plan_id=7, ledger_generation_id=pointer.id,
        status="FIXED_SNAPSHOT", config_snapshot={}, started_at=_STAMP,
    ))
    db_session.flush()
    _buy_owner(db_session, pointer, run_id=41)
    _snapshot(db_session, 1, "production_control_journal", "journal:v1", pointer.id, {"meta": {}})
    _snapshot(
        db_session, 2, "purchase_control_journal", "journal:v1", pointer.id,
        {"meta": {}, "rows": []},
    )
    db_session.flush()

    _capture_publisher(monkeypatch)
    with pytest.raises(LegacyEvidenceConflict, match="active current BUY owners"):
        publish_current_obligation_views_from_snapshots(db_session, pointer.id)


def test_mrp_evidence_is_the_latest_accepted_snapshot_not_the_runs_anchor(
    db_session, monkeypatch
):
    """The stand's shape: every fixed run is re-anchored to the newest
    obligation generation, while each run's own ``mrp_result`` snapshot stays
    at the generation that fixed that run."""

    _create_legacy_read_tables(db_session)
    fixing = _generation(db_session, "adapter-fixing")
    republished = _generation(db_session, "adapter-republished")
    obligation = _generation(db_session, "adapter-obligation")
    pointer = _generation(db_session, "adapter-physical-pointer")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=pointer.id))
    for run_id, plan_id in ((393, 6), (513, 7)):
        db_session.add(models.PlanningRun(
            run_id=run_id, source_plan_id=plan_id, ledger_generation_id=obligation.id,
            status="FIXED_SNAPSHOT", config_snapshot={}, started_at=_STAMP,
        ))
    db_session.add(models.PlanningRun(
        run_id=99, source_plan_id=9, ledger_generation_id=obligation.id,
        status="CLOSED", config_snapshot={}, started_at=_STAMP,
    ))
    db_session.flush()

    _snapshot(db_session, 1, "production_control_journal", "journal:v1", pointer.id, {"meta": {}})
    _read_row(
        db_session, 1, 1, row_key="work-item:1001",
        payload={"order_id": 9001, "product_id": 1001, "item_id": 1001},
    )
    _snapshot(
        db_session, 2, "purchase_control_journal", "journal:v1", pointer.id,
        {"meta": {}, "rows": [{"row_key": "buy:501", "item_id": 1001}]},
    )
    # Run 513 was fixed long ago and republished once; the newest copy wins.
    _snapshot(db_session, 3, "mrp_result", "run:513", fixing.id, {"origin": "fixing"})
    _read_row(
        db_session, 3, 3, row_key="mrp:stale", row_kind="purchase", sort_key="2026-01-01|1001",
        payload={"item_id": 1002, "source_mrp_requirement_id": 999, "bucket_date": "2026-01-01"},
    )
    _snapshot(db_session, 4, "mrp_result", "run:513", republished.id, {"origin": "republished"})
    _read_row(
        db_session, 4, 4, row_key="mrp:1", row_kind="purchase", sort_key="2026-09-11|1001",
        payload={"item_id": 1001, "source_mrp_requirement_id": 501, "bucket_date": "2026-09-11"},
    )
    # Run 393 is an old fixed plan: accepted evidence with no rows at all.
    _snapshot(db_session, 5, "mrp_result", "run:393", fixing.id, {"origin": "fixing"})
    # A CLOSED run must not be published even though its snapshot exists.
    _snapshot(db_session, 6, "mrp_result", "run:99", obligation.id, {"summary": {}})
    # Period evidence exists at the pointer and elsewhere; the pointer wins.
    for run_id, plan_id in ((393, 6), (513, 7)):
        _snapshot(
            db_session, 100 + run_id, "period_plan_execution",
            f"plan={plan_id};run={run_id}", pointer.id,
            {
                "plan": {"id": plan_id}, "run_id": run_id, "rows": [],
                "plan_output_rows": [], "origin": "pointer",
            },
        )
    _snapshot(
        db_session, 300, "period_plan_execution", "plan=7;run=513", fixing.id,
        {"plan": {"id": 7}, "run_id": 513, "rows": [], "plan_output_rows": [], "origin": "fixing"},
    )
    db_session.flush()

    captured = _capture_publisher(monkeypatch)
    publish_current_obligation_views_from_snapshots(db_session, pointer.id)

    assert set(captured["mrp_payloads"]) == {"393", "513"}
    mrp = captured["mrp_payloads"]["513"]
    assert mrp["origin"] == "republished"
    assert mrp["run_id"] == 513
    assert mrp["row_counts"]["purchase"] == 1
    assert len(mrp["rows"]) == 1
    assert mrp["rows"][0]["current_identity"].startswith("mrp-run:513:purchase:")
    assert mrp["rows"][0]["payload"]["item_id"] == 1001
    # An accepted but empty per-run payload is legitimate evidence.
    empty = captured["mrp_payloads"]["393"]
    assert empty["rows"] == []
    assert empty["row_counts"] == {
        "production": 0, "purchase": 0, "rework": 0, "capacity": 0
    }
    assert captured["period_payloads"]["plan:7:run:513"]["origin"] == "pointer"


def test_period_evidence_falls_back_to_the_latest_accepted_snapshot(db_session, monkeypatch):
    _create_legacy_read_tables(db_session)
    fixing = _generation(db_session, "adapter-period-fixing")
    obligation = _generation(db_session, "adapter-period-obligation")
    pointer = _generation(db_session, "adapter-period-pointer")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=pointer.id))
    db_session.add(models.PlanningRun(
        run_id=513, source_plan_id=7, ledger_generation_id=obligation.id,
        status="FIXED_SNAPSHOT", config_snapshot={}, started_at=_STAMP,
    ))
    db_session.flush()
    _snapshot(db_session, 1, "production_control_journal", "journal:v1", pointer.id, {"meta": {}})
    _read_row(
        db_session, 1, 1, row_key="work-item:1001",
        payload={"order_id": 9001, "product_id": 1001, "item_id": 1001},
    )
    _snapshot(
        db_session, 2, "purchase_control_journal", "journal:v1", pointer.id,
        {"meta": {}, "rows": [{"row_key": "buy:501", "item_id": 1001}]},
    )
    _snapshot(db_session, 3, "mrp_result", "run:513", fixing.id, {"summary": {}})
    _snapshot(
        db_session, 4, "period_plan_execution", "plan=7;run=513", fixing.id,
        {"plan": {"id": 7}, "run_id": 513, "rows": [], "plan_output_rows": [], "origin": "obligation"},
    )
    db_session.flush()

    captured = _capture_publisher(monkeypatch)
    publish_current_obligation_views_from_snapshots(db_session, pointer.id)

    assert captured["period_payloads"]["plan:7:run:513"]["origin"] == "obligation"


def test_missing_mrp_evidence_for_a_fixed_run_fails_closed(db_session, monkeypatch):
    _create_legacy_read_tables(db_session)
    obligation = _generation(db_session, "adapter-missing-obligation")
    pointer = _generation(db_session, "adapter-missing-pointer")
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=pointer.id))
    db_session.add(models.PlanningRun(
        run_id=513, source_plan_id=7, ledger_generation_id=obligation.id,
        status="FIXED_SNAPSHOT", config_snapshot={}, started_at=_STAMP,
    ))
    db_session.flush()
    _snapshot(db_session, 1, "production_control_journal", "journal:v1", pointer.id, {"meta": {}})
    _read_row(
        db_session, 1, 1, row_key="work-item:1001",
        payload={"order_id": 9001, "product_id": 1001, "item_id": 1001},
    )
    _snapshot(
        db_session, 2, "purchase_control_journal", "journal:v1", pointer.id,
        {"meta": {}, "rows": [{"row_key": "buy:501", "item_id": 1001}]},
    )
    db_session.flush()

    _capture_publisher(monkeypatch)
    with pytest.raises(LegacyEvidenceConflict, match="fixed run 513"):
        publish_current_obligation_views_from_snapshots(db_session, pointer.id)
