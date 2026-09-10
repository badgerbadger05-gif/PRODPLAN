from pathlib import Path

from app import models
from app.services.item_ledger.current_execution import (
    get_current_execution_scope,
    invalidate_current_execution_for_calendar_change,
    publish_current_execution_scope,
)
from app.services.specification_revision import record_specification_revisions


ROOT = Path(__file__).parents[2]


def test_r8_reference_mutations_invalidate_affected_current_scopes():
    planning_rates = (ROOT / "backend/app/routers/planning_rates.py").read_text(encoding="utf-8")
    resources = (ROOT / "backend/app/routers/resources.py").read_text(encoding="utf-8")
    manual = (ROOT / "backend/app/services/item_ledger/drum_manual_move.py").read_text(encoding="utf-8")
    assert "invalidate_current_execution_scope" in planning_rates
    assert "invalidate_current_execution_scope" in resources
    assert "invalidate_current_execution_scope" in manual
    assert planning_rates.count('scope_key="shelf:all-live-mrps"') >= 3
    assert planning_rates.count('scope_key="drum:all-live-plans"') >= 2
    assert resources.count('scope_key="drum:all-live-plans"') >= 1


def test_r8_calendar_hook_invalidates_only_calendar_dependents(db_session):
    for kind, scope in (
        ("assembly_readiness", "assembly:all-live-plans"),
        ("drum_schedule", "drum:all-live-plans"),
        ("assembly_queue", "assembly:all-live-plans"),
    ):
        publish_current_execution_scope(
            db_session,
            source_revision="accepted:g1",
            scope_key=scope,
            entity_kinds=(kind,),
            rows=[{
                "entity_kind": kind,
                "business_identity": f"{kind}:1",
                "scope_key": scope,
                "payload": {"value": "1"},
            }],
        )
    invalidated = invalidate_current_execution_for_calendar_change(
        db_session,
        source_revision="calendar:v2",
    )
    assert set(invalidated) == {"assembly_readiness", "drum_schedule"}
    assert get_current_execution_scope(
        db_session, entity_kind="assembly_queue", scope_key="assembly:all-live-plans"
    ).result_ready is True
    assert get_current_execution_scope(
        db_session, entity_kind="drum_schedule", scope_key="drum:all-live-plans"
    ).result_ready is False


def test_r8_specification_revision_invalidates_only_on_semantic_change(db_session):
    item = models.Item(item_code="R8-SPEC-ITEM", item_name="R8 spec item")
    spec = models.Specification(spec_ref1c="r8-spec", spec_code="R8", spec_name="R8")
    db_session.add_all([item, spec])
    db_session.flush()
    component = models.SpecComponent(spec_id=spec.spec_id, item_id=item.item_id, quantity="1")
    db_session.add(component)
    db_session.flush()
    for kind, scope in (
        ("assembly_queue", "assembly:all-live-plans"),
        ("assembly_readiness", "assembly:all-live-plans"),
        ("drum_schedule", "drum:all-live-plans"),
        ("shelf_projection", "shelf:all-live-mrps"),
    ):
        publish_current_execution_scope(
            db_session,
            source_revision="accepted:g1",
            scope_key=scope,
            entity_kinds=(kind,),
            rows=[{
                "entity_kind": kind,
                "business_identity": f"{kind}:1",
                "scope_key": scope,
                "payload": {"value": "1"},
            }],
        )
    record_specification_revisions(db_session, [spec.spec_id], previous_hash_by_id={})
    old_hash = str(spec.content_hash)
    assert get_current_execution_scope(
        db_session, entity_kind="assembly_queue", scope_key="assembly:all-live-plans"
    ).result_ready is True
    component.quantity = "2"
    record_specification_revisions(
        db_session,
        [spec.spec_id],
        previous_hash_by_id={int(spec.spec_id): old_hash},
    )
    assert get_current_execution_scope(
        db_session, entity_kind="assembly_queue", scope_key="assembly:all-live-plans"
    ).result_ready is False
