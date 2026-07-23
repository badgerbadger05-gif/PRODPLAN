from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app import models
from app.services import planning_comparison as service


def _run_with_outputs(db, *, started_at=None):
    item = models.Item(
        item_code="A-100", item_name="Part", item_ref1c="AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
    )
    db.add(item)
    db.flush()
    run = models.PlanningRun(
        started_at=started_at or datetime(2026, 7, 23, 8, 0),
        finished_at=datetime(2026, 7, 23, 8, 5),
        status="COMPLETED", config_snapshot={}, period_from=date(2026, 7, 23), period_to=date(2026, 7, 30),
    )
    db.add(run)
    db.flush()
    db.add_all([
        models.PlannedOrder(
            run_id=run.run_id, item_id=item.item_id, requested_qty=Decimal("1.100"),
            planned_qty=Decimal("1.100"), qty=Decimal("1.100"), need_date=date(2026, 7, 24),
            start_date=date(2026, 7, 23), finish_date=date(2026, 7, 24), bucket_date=date(2026, 7, 24),
        ),
        models.PlannedOrder(
            run_id=run.run_id, item_id=item.item_id, requested_qty=Decimal("2.200"),
            planned_qty=Decimal("2.200"), qty=Decimal("2.200"), need_date=date(2026, 7, 24),
            start_date=date(2026, 7, 23), finish_date=date(2026, 7, 24), bucket_date=date(2026, 7, 24),
        ),
        models.PlannedPurchase(
            run_id=run.run_id, item_id=item.item_id, requested_qty=Decimal("4.250"),
            planned_qty=Decimal("4.250"), qty=Decimal("4.250"), need_date=date(2026, 7, 25),
            order_date=date(2026, 7, 23), lead_time_days=2, bucket_date=date(2026, 7, 25),
            supplier_ref1c="BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
        ),
    ])
    db.commit()
    return run


def test_canonical_rows_use_external_keys_and_decimal_safe_aggregation(db_session):
    run = _run_with_outputs(db_session)

    rows = service.canonical_result_rows(db_session, run.run_id)

    production = next(row for row in rows if row["result_kind"] == "production")
    assert production["item_key"] == "ref1c:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert Decimal(production["quantity"]) == Decimal("3.300")
    assert f"run:{run.run_id}" not in production["canonical_key"]
    purchase = next(row for row in rows if row["result_kind"] == "purchase")
    assert "supplier:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in purchase["canonical_key"]
    assert len(production["raw_payload_hash"]) == 64


def test_cutoff_grades_exact_near_and_invalid():
    base = {
        "fingerprint": "same",
        "watermarks": {"stock": "2026-07-23T08:00:00+00:00"},
        "run": {"period_from": "2026-07-23", "period_to": "2026-07-30"},
    }
    assert service.cutoff_grade(base, dict(base), 60)[0] == "exact"

    near = {
        "fingerprint": "other",
        "watermarks": {"stock": "2026-07-23T08:00:30+00:00"},
        "run": dict(base["run"]),
    }
    assert service.cutoff_grade(base, near, 60)[0] == "near"
    assert service.cutoff_grade(base, near, 10)[0] == "invalid"


def test_input_fingerprint_changes_for_critical_planning_inputs(db_session):
    _run_with_outputs(db_session)
    item = db_session.query(models.Item).filter_by(item_code="A-100").one()
    baseline = service.input_fingerprint(db_session)
    plan = models.ProductionPlanHeader(
        name="July", period_from=date(2026, 7, 23), period_to=date(2026, 7, 30),
        status="fixed",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(models.ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 7, 24),
        qty=Decimal("2"),
    ))
    db_session.commit()
    demand = service.input_fingerprint(db_session)
    assert demand["components"]["period_plan"] != baseline["components"]["period_plan"]
    spec = models.Specification(spec_code="SP-A", spec_name="Spec A")
    db_session.add(spec)
    db_session.flush()
    db_session.add(models.SpecComponent(
        spec_id=spec.spec_id, item_id=item.item_id, quantity=Decimal("1"),
    ))
    db_session.commit()
    bom = service.input_fingerprint(db_session)
    assert bom["components"]["bom_components"] != demand["components"]["bom_components"]
    db_session.add(models.ProductionResource(resource_name="Laser", capacity=Decimal("8")))
    db_session.commit()
    capacity = service.input_fingerprint(db_session)
    assert capacity["components"]["resources"] != bom["components"]["resources"]
    db_session.add(models.PlanningConfigVersion(
        version=1, is_active=True, config={"warehouse_policy": "selected"},
    ))
    db_session.commit()
    settings = service.input_fingerprint(db_session)
    assert settings["components"]["planning_settings"] != capacity["components"]["planning_settings"]
    assert len({baseline["fingerprint"], demand["fingerprint"], bom["fingerprint"],
                capacity["fingerprint"], settings["fingerprint"]}) == 5


def test_capture_is_append_only_and_idempotent(db_session, monkeypatch):
    _run_with_outputs(db_session)
    shadow = service.input_fingerprint(db_session, include_results=True)
    stable = {**shadow, "results": [dict(row) for row in shadow["results"]]}
    stable["results"][0]["quantity"] = "9.300000"

    def fake_get(_base, path, query=None):
        if path.endswith("input-fingerprint"):
            return stable
        if path.endswith("/runs"):
            return {"rows": [{"run_id": 41}], "total": 1}
        if path.endswith("/results/41"):
            return {"run_id": 41}
        return {"status": "ok", "jobs": []}

    monkeypatch.setenv("STABLE_PRODPLAN_API_URL", "http://stable.internal")
    monkeypatch.setattr(service, "_stable_get", fake_get)

    first = service.capture(db_session, capture_key="shift-20260723")
    second = service.capture(db_session, capture_key="shift-20260723")

    assert first["id"] == second["id"]
    assert db_session.query(models.PlanningComparisonBatch).count() == 1
    assert db_session.query(models.PlanningComparisonEvent).count() == 1
    assert db_session.query(models.PlanningComparisonSnapshot).count() == 4
    assert first["cutoff_grade"] == "exact"
    assert any(row["classification"] == "changed" for row in first["diffs"])


def test_capture_falls_back_to_paginated_legacy_stable_api(db_session, monkeypatch):
    _run_with_outputs(db_session)
    shadow = service.input_fingerprint(db_session, include_results=True)
    shadow["watermarks"] = {
        "stock": "2026-07-23T08:00:05+00:00",
        "production_orders": "2026-07-23T08:00:05+00:00",
        "supplier_orders": "2026-07-23T08:00:05+00:00",
    }
    calls = []

    def fake_get(_base, path, query=None):
        calls.append((path, dict(query or {})))
        if path.endswith("input-fingerprint"):
            raise service.StableAPIReadError(path, RuntimeError("not found"), 404)
        if path.endswith("/sync/auto/status"):
            return {
                "status": "ok",
                "jobs": [
                    {"id": "stock", "last_run_at": "2026-07-23T08:00:00+00:00"},
                    {"id": "productionOrders", "last_run_at": "2026-07-23T08:00:00+00:00"},
                    {"id": "supplierOrders", "last_run_at": "2026-07-23T08:00:00+00:00"},
                ],
            }
        if path.endswith("/plan/runs"):
            return {"rows": [{
                "run_id": 41, "status": "SUCCESS",
                "started_at": "2026-07-23T08:00:00",
                "period_from": "2026-07-23", "period_to": "2026-07-30",
            }], "total": 1}
        if path.endswith("/results/41"):
            return {"run_id": 41}
        if path.endswith("/production"):
            offset = int((query or {}).get("offset", 0))
            rows = [{
                "item_id": 777, "qty": "9.3", "bucket_date": "2026-07-24",
            }] if offset == 0 else []
            return {"rows": rows, "total": 1, "limit": 1, "offset": offset}
        if path.endswith("/purchases"):
            return {"rows": [{
                "item_code": "A-100", "qty": "4.25", "bucket_date": "2026-07-25",
                "supplier_ref1c": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
            }], "total": 1}
        if path.endswith("/rework"):
            return {"rows": [], "total": 0}
        if path.endswith("/items/777"):
            return {
                "item_id": 777, "item_code": "A-100",
                "item_ref1c": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            }
        raise AssertionError(path)

    monkeypatch.setenv("STABLE_PRODPLAN_API_URL", "http://stable.internal")
    monkeypatch.setenv("PLANNING_COMPARISON_PAGE_SIZE", "1")
    monkeypatch.setattr(service, "_stable_get", fake_get)
    monkeypatch.setattr(service, "input_fingerprint", lambda *_args, **_kwargs: shadow)

    captured = service.capture(db_session, capture_key="legacy-stable")

    assert captured["cutoff_grade"] == "near"
    assert "no input fingerprint" in captured["cutoff_reason"]
    assert any(row["classification"] == "changed" for row in captured["diffs"])
    assert any(path.endswith("/production") for path, _ in calls)
    snapshots = db_session.query(models.PlanningComparisonSnapshot).filter_by(
        batch_id=captured["id"],
    ).all()
    assert len(snapshots) == 5
    raw = next(row.payload for row in snapshots if row.snapshot_kind == "legacy_result_pages")
    assert raw["result_pages"]["production"]["pages"][0]["rows"][0]["qty"] == "9.3"
    assert raw["items"]["777"]["item_code"] == "A-100"


def test_legacy_cutoff_is_never_exact_and_rejects_missing_watermark():
    stable = {
        "watermarks": {"stock": "2026-07-23T08:00:00+00:00"},
        "run": {"period_from": "2026-07-23", "period_to": "2026-07-30"},
    }
    shadow = {
        "watermarks": {"stock": "2026-07-23T08:00:00+00:00"},
        "run": dict(stable["run"]),
    }
    grade, reason = service._legacy_cutoff_grade(stable, shadow, 60)
    assert grade == "near"
    assert "no input fingerprint" in reason

    shadow["watermarks"]["stock"] = None
    assert service._legacy_cutoff_grade(stable, shadow, 60)[0] == "invalid"
