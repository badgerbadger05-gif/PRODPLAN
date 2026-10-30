from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app import models
from app.services.production_material_custody_projection import (
    PreparedCurrentMaterialCustodyBasisConflict,
    build_material_custody_projection,
    install_prepared_current_material_custody,
    prepare_current_material_custody,
)
from tests.services.test_production_material_custody_projection import (
    _building_generation,
    _event,
    _generation,
    _manifest,
    _product,
    _seed_projection,
)


def _prepared_target(db_session, suffix: str):
    cutoff = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
    base = _generation(db_session, key=f"custody-prepare-base-{suffix}", cutoff=cutoff)
    product, _parent, component = _product(
        db_session, item_code=f"CUSTODY-PREP-{suffix}"
    )
    _manifest(db_session, generation_id=base.id, source_event_high_watermark_id=0)
    _seed_projection(
        db_session,
        generation_id=base.id,
        product_id=product.product_id,
        component_id=component.item_id,
        qty=5,
        source_event_high_watermark_id=0,
    )
    target = _building_generation(
        db_session,
        key=f"custody-prepare-target-{suffix}",
        cutoff=cutoff + timedelta(days=1),
        parent=base,
    )
    build_material_custody_projection(db_session, ledger_generation_id=target.id)
    target.status = "accepted"
    target.accepted_at = target.cutoff
    return base, target, product, component


def test_custody_prepare_is_read_only_and_install_promotes_same_rows(db_session):
    _base, target, product, component = _prepared_target(db_session, "readonly")
    db_session.flush()
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper())

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        prepared = prepare_current_material_custody(
            db_session, ledger_generation_id=int(target.id)
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)

    assert not db_session.new
    assert not db_session.dirty
    assert all("FOR UPDATE" not in statement for statement in statements)
    assert all(
        not statement.lstrip().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in statements
    )
    staged = db_session.query(models.ProductionMaterialCustodyProjection).filter_by(
        ledger_generation_id=int(target.id)
    ).one()
    assert staged.is_current is False

    assert install_prepared_current_material_custody(db_session, prepared) == 1
    current = db_session.query(models.ProductionMaterialCustodyProjection).filter_by(
        ledger_generation_id=int(target.id), is_current=True
    ).one()
    assert int(current.product_id) == int(product.product_id)
    assert int(current.component_item_id) == int(component.item_id)
    assert current.reserved_qty == staged.reserved_qty


def test_custody_install_rejects_pointer_change_without_overwrite(db_session):
    _base, target, _product_row, _component = _prepared_target(db_session, "pointer")
    db_session.flush()
    prepared = prepare_current_material_custody(
        db_session, ledger_generation_id=int(target.id)
    )
    pointer = db_session.get(models.PlanningTruthState, 1)
    pointer.current_generation_id = int(target.id)
    db_session.flush()

    with pytest.raises(PreparedCurrentMaterialCustodyBasisConflict, match="pointer changed"):
        install_prepared_current_material_custody(db_session, prepared)

    assert db_session.query(models.ProductionMaterialCustodyProjection).filter_by(
        ledger_generation_id=int(target.id), is_current=True
    ).count() == 0


def test_custody_install_rejects_late_event_after_prepare(db_session):
    base, target, product, component = _prepared_target(db_session, "late-event")
    db_session.flush()
    prepared = prepare_current_material_custody(
        db_session, ledger_generation_id=int(target.id)
    )
    _event(
        db_session,
        source_kind="issue_created",
        issue_id=0,
        product_id=product.product_id,
        component_id=component.item_id,
        location="workshop",
        warehouse="WH-MAIN",
        qty=1,
        key="custody:prepare:late-event",
        effective_at=base.cutoff + timedelta(hours=1),
    )
    db_session.flush()

    with pytest.raises(PreparedCurrentMaterialCustodyBasisConflict, match="watermark changed"):
        install_prepared_current_material_custody(db_session, prepared)

    assert db_session.query(models.ProductionMaterialCustodyProjection).filter_by(
        ledger_generation_id=int(target.id), is_current=True
    ).count() == 0
