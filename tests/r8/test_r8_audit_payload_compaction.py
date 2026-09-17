"""R8: the change audit proves what changed without a second copy of the blob.

``CurrentExecutionRow.payload`` stays complete — the screens read it.  The
change audit answers a different question and keeps every business scalar
verbatim while storing the heavy derived presentation sub-objects
(``readiness_curve``, ``blocking_manifest``, ``action_manifest``, the journal
snapshots, ``queue_links``, purchase horizons) as a digest marker.

The semantic change detection itself is untouched: the same republishes create
the same rows and the same number of audit rows as before.
"""

from app import models
from app.services.item_ledger.current_execution import (
    AUDIT_DIGEST_BYTES_KEY,
    AUDIT_DIGEST_KEY,
    AUDIT_LARGE_VALUE_BYTES,
    audit_digest_marker,
    compact_audit_payload,
    publish_current_execution_scope,
)


SCOPE = "assembly:all-live-plans"


def _curve(points: int, qty: str = "4.000"):
    return [
        {
            "horizon": "launch",
            "ordinal": index,
            "cumulative_qty": qty,
            "required_actions": [{"item_id": 400 + index, "qty": qty}],
        }
        for index in range(points)
    ]


def _readiness_row(
    identity: str = "plan-line:301",
    *,
    qty: str = "4.000",
    status: str = "partial",
    curve_points: int = 3,
    unavailable_reasons=None,
):
    return {
        "entity_kind": "assembly_readiness",
        "business_identity": identity,
        "scope_key": SCOPE,
        "payload": {
            "plan_id": 12,
            "plan_line_id": 301,
            "run_id": 7,
            "item_id": 401,
            "status": status,
            "open_qty": qty,
            "ready_qty": "0.000",
            "blocker_count": 2,
            "original_priority": [2026, 4, 11],
            "readiness_date": None,
            "readiness_curve": _curve(curve_points, qty),
            "blocking_manifest": [{"item_id": 500 + i, "missing_qty": qty} for i in range(curve_points)],
            "action_manifest": [{"action": "launch", "item_id": 401}],
            "unavailable_reasons": list(unavailable_reasons or []),
        },
    }


def _publish(db, rows, revision, *, kinds=("assembly_readiness",), scope=SCOPE):
    result = publish_current_execution_scope(
        db,
        source_revision=revision,
        scope_key=scope,
        rows=rows,
        entity_kinds=kinds,
    )
    db.flush()
    return result


def _changes(db):
    return db.query(models.CurrentExecutionChange).order_by(
        models.CurrentExecutionChange.id.asc()
    ).all()


# --------------------------------------------------------------------------
# The publisher
# --------------------------------------------------------------------------


def test_r8_audit_replaces_the_readiness_curve_with_a_digest_marker(db_session):
    _publish(db_session, [_readiness_row()], "physical:g1")

    change = _changes(db_session)[-1]
    assert change.operation == "insert"
    after = dict(change.after_payload)
    # The heavy derived sub-objects are markers, not bodies.
    for key in ("readiness_curve", "blocking_manifest", "action_manifest"):
        marker = after[key]
        assert set(marker) == {AUDIT_DIGEST_KEY, AUDIT_DIGEST_BYTES_KEY}
        assert str(marker[AUDIT_DIGEST_KEY]).startswith("sha256:")
        assert int(marker[AUDIT_DIGEST_BYTES_KEY]) > 0
    # Every business scalar survives verbatim.
    assert after["status"] == "partial"
    assert after["open_qty"] == "4.000"
    assert after["blocker_count"] == 2
    assert after["plan_line_id"] == 301
    assert after["original_priority"] == [2026, 4, 11]
    # The current row itself still carries the full payload for the screens.
    row = db_session.query(models.CurrentExecutionRow).one()
    assert len(row.payload["readiness_curve"]) == 3


def test_r8_a_changed_curve_writes_one_change_row_with_differing_digests(db_session):
    _publish(db_session, [_readiness_row(curve_points=3)], "physical:g1")
    before_count = db_session.query(models.CurrentExecutionChange).count()

    _publish(db_session, [_readiness_row(curve_points=5)], "physical:g2")

    changes = _changes(db_session)
    assert len(changes) == before_count + 1
    change = changes[-1]
    assert change.operation == "update"
    before, after = dict(change.before_payload), dict(change.after_payload)
    assert "horizon" not in str(before["readiness_curve"])
    assert "horizon" not in str(after["readiness_curve"])
    # "What changed" stays provable: the curve digest differs, the untouched
    # action manifest keeps the very same digest.
    assert before["readiness_curve"][AUDIT_DIGEST_KEY] != after["readiness_curve"][AUDIT_DIGEST_KEY]
    assert before["action_manifest"][AUDIT_DIGEST_KEY] == after["action_manifest"][AUDIT_DIGEST_KEY]
    assert before["status"] == after["status"] == "partial"


def test_r8_a_scalar_change_is_visible_in_the_audit_before_and_after(db_session):
    _publish(db_session, [_readiness_row(status="partial", qty="4.000")], "physical:g1")

    _publish(db_session, [_readiness_row(status="blocked", qty="4.000")], "physical:g2")

    change = _changes(db_session)[-1]
    assert change.before_payload["status"] == "partial"
    assert change.after_payload["status"] == "blocked"
    # The unchanged heavy parts are proved equal by their digest.
    assert (
        change.before_payload["blocking_manifest"][AUDIT_DIGEST_KEY]
        == change.after_payload["blocking_manifest"][AUDIT_DIGEST_KEY]
    )


def test_r8_closing_a_row_stores_the_compact_before_payload(db_session):
    _publish(db_session, [_readiness_row("plan-line:301"), _readiness_row("plan-line:302")], "physical:g1")

    _publish(db_session, [_readiness_row("plan-line:301")], "physical:g2")

    close = [change for change in _changes(db_session) if change.operation == "close"]
    assert len(close) == 1
    before = dict(close[0].before_payload)
    assert before["readiness_curve"][AUDIT_DIGEST_KEY].startswith("sha256:")
    assert before["status"] == "partial"
    assert close[0].after_payload is None


def test_r8_a_payload_without_heavy_keys_is_stored_as_before(db_session):
    row = {
        "entity_kind": "assembly_queue",
        "business_identity": "plan-line:301",
        "scope_key": SCOPE,
        "payload": {
            "plan_id": 12,
            "plan_line_id": 301,
            "assembly_remaining_qty": "4.000",
            "period_from": "2026-04-01",
            "period_to": "2026-04-30",
        },
    }
    _publish(db_session, [row], "physical:g1", kinds=("assembly_queue",))

    change = _changes(db_session)[-1]
    assert dict(change.after_payload) == row["payload"]


def test_r8_the_no_op_republish_still_writes_no_audit_row(db_session):
    _publish(db_session, [_readiness_row()], "physical:g1")
    changes = db_session.query(models.CurrentExecutionChange).count()

    result = _publish(db_session, [_readiness_row()], "physical:g2")

    assert result.idempotent is True
    assert db_session.query(models.CurrentExecutionChange).count() == changes


# --------------------------------------------------------------------------
# The compaction rule itself
# --------------------------------------------------------------------------


def test_r8_digest_is_equal_for_equal_content_and_differs_for_different_content():
    assert audit_digest_marker(_curve(3)) == audit_digest_marker(_curve(3))
    assert audit_digest_marker(_curve(3)) != audit_digest_marker(_curve(4))
    # Key order in the source object is transport noise, not content.
    assert audit_digest_marker({"a": 1, "b": 2}) == audit_digest_marker({"b": 2, "a": 1})


def test_r8_small_reason_codes_stay_readable_and_a_large_blob_is_digested():
    small = compact_audit_payload(
        {"status": "blocked", "unavailable_reasons": ["FROZEN_SPEC_AMBIGUOUS"]},
        "assembly_readiness",
    )
    assert small["unavailable_reasons"] == ["FROZEN_SPEC_AMBIGUOUS"]

    large_reasons = [{"code": "X", "detail": "y" * 80} for _ in range(60)]
    large = compact_audit_payload(
        {"status": "blocked", "unavailable_reasons": large_reasons},
        "assembly_readiness",
    )
    assert set(large["unavailable_reasons"]) == {AUDIT_DIGEST_KEY, AUDIT_DIGEST_BYTES_KEY}
    assert large["unavailable_reasons"][AUDIT_DIGEST_BYTES_KEY] > AUDIT_LARGE_VALUE_BYTES


def test_r8_journal_and_purchase_snapshots_are_digested_per_entity_kind():
    journal = compact_audit_payload(
        {
            "order_ref1c": "ORD-1",
            "line_status": "in_progress",
            "_route_sheet_snapshot": {"lines": [1, 2, 3]},
            "material_coverage_snapshot": {"items": [4, 5]},
        },
        "production_control_journal",
    )
    assert journal["order_ref1c"] == "ORD-1"
    assert AUDIT_DIGEST_KEY in journal["_route_sheet_snapshot"]
    assert AUDIT_DIGEST_KEY in journal["material_coverage_snapshot"]

    purchase = compact_audit_payload(
        {
            "row_key": "purchase:1",
            "to_order_qty": "5.000",
            "horizon_buckets": [{"d": 1}],
            "slices": [{"d": 2}],
            "materialization_input": {"d": 3},
        },
        "purchase_control_journal",
    )
    assert purchase["to_order_qty"] == "5.000"
    for key in ("horizon_buckets", "slices", "materialization_input"):
        assert AUDIT_DIGEST_KEY in purchase[key]

    period = compact_audit_payload(
        {"current_identity": "req:1", "status": "partial", "queue_links": [{"href": "#/x"}]},
        "period_plan_execution",
    )
    assert period["status"] == "partial"
    assert AUDIT_DIGEST_KEY in period["queue_links"]

    # An entity kind with no declared heavy keys is never touched.
    untouched = {"stages": [{"a": 1}], "flags": {"b": 2}}
    assert compact_audit_payload(untouched, "mrp_result") == untouched
