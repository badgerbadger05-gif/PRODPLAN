import pytest
from fastapi import HTTPException
from app import models
from app.services.item_ledger.current_execution import publish_current_production_control_from_payload
from app.services.production_control_journal_projection import prepare_current_work_materials
from app.routers.production_control import get_work_item_materials
from tests.services.test_make_work_item_materialization import _scope


def _current(db):
    work, req, owner = _scope(db)
    identity = f"mrp-reservation:{owner.current_identity}"
    publish_current_production_control_from_payload(db, work.ledger_generation_id, {
        'meta': {}, 'rows': [{'current_identity': identity, 'item_id': work.item_id,
        'source_mrp_requirement_id': req.id, 'source_run_id': work.run_id,
        'launchable_qty': 8, 'order_number': f'MRP-R-{req.id}',
        'material_coverage_snapshot': {'qty': 8, 'line_quantity': 8, 'components': []}}],
    })
    db.commit()
    return work, identity, f'accepted:g{work.ledger_generation_id}:production_control_journal'


def test_quantity_command_persists_preview_and_get_only_reads_it(db_session, monkeypatch):
    work, identity, revision = _current(db_session)
    calls = []
    def preview(*args, **kw):
        calls.append(kw['quantity'])
        return {'qty': kw['quantity'], 'line_quantity': kw['quantity'], 'components': [],
                'coverage_status': 'ready', 'ledger_generation_id': work.ledger_generation_id}
    monkeypatch.setattr('app.services.production_control_material_availability.preview_make_work_item_materials', preview)
    prepared = prepare_current_work_materials(db_session, work_item_id=work.id, qty=3,
                  current_identity=identity, expected_source_revision=revision)
    db_session.commit()
    assert prepared['qty'] == 3
    assert calls == [3]
    monkeypatch.setattr('app.services.production_control_material_availability.preview_make_work_item_materials',
                        lambda *a, **k: pytest.fail('GET must never calculate BOM'))
    result = get_work_item_materials(work.id, qty=3, ledger_generation_id=None,
             current_identity=identity, expected_source_revision=revision, db=db_session)
    assert result['qty'] == 3
    assert 'line_quantity' not in result
    assert prepare_current_work_materials(db_session, work_item_id=work.id, qty=3,
           current_identity=identity, expected_source_revision=revision)['qty'] == 3
    scope = db_session.query(models.CurrentExecutionScope).filter_by(entity_kind='production_control_journal').one()
    scope.source_revision = 'new-revision'
    db_session.commit()
    with pytest.raises(HTTPException) as error:
        get_work_item_materials(work.id, qty=3, ledger_generation_id=None,
            current_identity=identity, expected_source_revision='new-revision', db=db_session)
    assert error.value.status_code == 503


@pytest.mark.parametrize('qty', [0, -1, 9, float('inf')])
def test_preparation_rejects_invalid_quantity_before_calculation(db_session, monkeypatch, qty):
    work, identity, revision = _current(db_session)
    monkeypatch.setattr('app.services.production_control_material_availability.preview_make_work_item_materials',
                        lambda *a, **k: pytest.fail('invalid quantities must not calculate BOM'))
    with pytest.raises(HTTPException) as error:
        prepare_current_work_materials(db_session, work_item_id=work.id, qty=qty,
                  current_identity=identity, expected_source_revision=revision)
    assert error.value.status_code == 400


def test_prepare_rejects_stale_revision_before_calculation(db_session, monkeypatch):
    work, identity, revision = _current(db_session)
    monkeypatch.setattr('app.services.production_control_material_availability.preview_make_work_item_materials',
                        lambda *a, **k: pytest.fail('stale revision must not calculate'))
    with pytest.raises(HTTPException) as error:
        prepare_current_work_materials(db_session, work_item_id=work.id, qty=3,
                  current_identity=identity, expected_source_revision='old')
    assert error.value.status_code == 409


def test_materialization_and_read_model_publication_rollback_as_one_command(db_session, monkeypatch):
    from app.routers.production_control import _materialize_orders_from_work_items, OrdersFromWorkItemsPayload
    work, identity, revision = _current(db_session)
    before = db_session.query(models.ProductionOrder).count()
    def publication_failure(*args, **kwargs):
        raise ValueError('simulated read-model failure')
    monkeypatch.setattr('app.services.production_control_journal_projection.publish_local_make_changes', publication_failure)
    with pytest.raises(HTTPException):
        _materialize_orders_from_work_items(OrdersFromWorkItemsPayload(
            work_items=[{'work_item_id': work.id, 'launch_qty': 3, 'expected_materialized_qty': 0}],
            current_identities=[identity], expected_source_revision=revision,
        ), db_session)
    db_session.rollback()
    assert db_session.query(models.ProductionOrder).count() == before


def test_command_worker_does_not_race_physical_publication(monkeypatch):
    from app.services import production_control_command_worker as worker
    from app.services.item_ledger import physical_refresh_orchestrator as physical
    monkeypatch.setattr(physical, '_acquire_lifecycle_lock', lambda db: None)
    monkeypatch.setattr(worker, '_execute', lambda *a: pytest.fail('busy Ledger must not write'))
    with pytest.raises(HTTPException) as error:
        worker.execute(object(), 'materials', {})
    assert error.value.status_code == 409


def test_command_worker_rolls_back_before_releasing_shared_lock(monkeypatch):
    from types import SimpleNamespace
    from app.services import production_control_command_worker as worker
    from app.services.item_ledger import physical_refresh_orchestrator as physical
    calls = []
    monkeypatch.setattr(physical, '_acquire_lifecycle_lock', lambda db: True)
    monkeypatch.setattr(physical, '_release_lifecycle_lock', lambda lock: calls.append('release'))
    def fail(*args):
        raise ValueError('failed command')
    monkeypatch.setattr(worker, '_execute', fail)
    with pytest.raises(ValueError):
        worker.execute(SimpleNamespace(rollback=lambda: calls.append('rollback')), 'materialize', {})
    assert calls == ['rollback', 'release']


def test_current_material_worker_uses_compact_custody_without_historical_replay(db_session, monkeypatch):
    from app.services.production_control_journal import materialize_make_work_items
    from app.services.production_control_material_availability import preview_materials_bulk
    from app.services.production_material_custody import MaterialCustodyState
    work, _, _ = _scope(db_session)
    created = materialize_make_work_items(db_session, [work.id], launch_requests={
        work.id: {'launch_qty': 3, 'expected_materialized_qty': 0},
    })['created'][0]
    monkeypatch.setattr('app.services.production_material_custody_projection.load_material_custody_projection',
        lambda *a, **k: pytest.fail('current commands must not replay custody history'))
    calls = []
    def compact(*args, **kwargs):
        calls.append(kwargs['consumer'])
        return work.ledger_generation_id, MaterialCustodyState()
    monkeypatch.setattr('app.services.production_material_custody_projection.load_compact_current_material_custody', compact)
    result = preview_materials_bulk(db_session, [created['product_id']],
                 ledger_generation_id=work.ledger_generation_id, _current_only=True)
    assert result[created['product_id']]['qty'] == 3
    assert calls == ['production.current_materials_bulk']
