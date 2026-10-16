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


@pytest.mark.parametrize('routing', ['NO_SPEC', 'NO_PRODUCTION_KIND', 'KIND_NOT_BOUND', 'NO_WAREHOUSE_BINDING', 'OK'])
def test_launch_checks_workshop_routing_before_materializing(db_session, monkeypatch, routing):
    from app.routers.production_control import OrdersFromWorkItemsPayload, _materialize_orders_from_work_items

    work, identity, revision = _current(db_session)
    saved = db_session.query(models.CurrentExecutionRow).filter_by(business_identity=identity).one()
    if routing != 'NO_SPEC':
        spec = models.Specification(spec_name='Combined weld and paint')
        db_session.add(spec)
        if routing != 'NO_PRODUCTION_KIND':
            kind = models.ProductionKind(ref_1c='combined-weld-paint-kind', name='Combined weld and paint')
            db_session.add(kind)
            db_session.flush()
            spec.production_kind_id = kind.id
            if routing != 'KIND_NOT_BOUND':
                resource = models.ProductionResource(resource_name='Welding')
                db_session.add(resource)
                db_session.flush()
                db_session.add(models.ResourceProductionKind(resource_id=resource.resource_id, production_kind_id=kind.id))
                if routing == 'OK':
                    db_session.add(models.WorkshopWarehouseBinding(workshop_id=resource.resource_id, warehouse_ref1c='workshop-warehouse'))
        db_session.flush()
        saved.payload = {**saved.payload, 'spec_id': spec.spec_id}
    db_session.commit()
    frozen = tuple(db_session.query(models.ReservationEntry).one().__dict__.get(key) for key in (
        'reserved_qty', 'replenishment_required_qty', 'replenishment_received_qty'))
    calls = []
    def materialize(*args, **kwargs):
        calls.append('materialize')
        return {'created': [], 'reused': [], 'skipped': [], 'errors': []}
    monkeypatch.setattr('app.routers.production_control.materialize_make_work_items', materialize)
    payload = OrdersFromWorkItemsPayload(work_items=[{
        'work_item_id': work.id, 'launch_qty': 3, 'expected_materialized_qty': 0,
    }], current_identities=[identity], expected_source_revision=revision, require_workshop_routing=True)
    if routing == 'OK':
        assert _materialize_orders_from_work_items(payload, db_session)['errors'] == []
        assert calls == ['materialize']
    else:
        with pytest.raises(HTTPException) as error:
            _materialize_orders_from_work_items(payload, db_session)
        assert error.value.status_code == 400
        assert routing in error.value.detail
        assert calls == []
    assert db_session.query(models.ProductionOrder).count() == 0
    assert frozen == tuple(db_session.query(models.ReservationEntry).one().__dict__.get(key) for key in (
        'reserved_qty', 'replenishment_required_qty', 'replenishment_received_qty'))
    assert saved.payload['launchable_qty'] == 8


@pytest.fixture
def http_db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


@pytest.mark.parametrize('stale', [False, True])
def test_http_refresh_current_dispatches_validated_executor_to_worker(http_db_session, monkeypatch, stale):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.routers.production_control import router

    db_session = http_db_session
    work, _, revision = _current(db_session)
    identity = 'production:order:42'
    publish_current_production_control_from_payload(db_session, work.ledger_generation_id, {
        'meta': {}, 'rows': [{'current_identity': identity, 'product_id': 42,
                             'order_id': 7, 'item_id': work.item_id}],
    })
    db_session.commit()
    calls = []
    def worker(action, payload):
        calls.append((action, payload))
        return {'status': 'refreshed', 'product_ids': [42], 'current_identities': [identity], 'source_revision': revision}
    monkeypatch.setattr('app.services.production_control_command_worker.run_command', worker)
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        response = client.post('/api/v1/production-control/orders/refresh-current', json={
            'product_ids': [42], 'current_identities': [identity],
            'expected_source_revision': 'stale' if stale else revision,
        })
    if stale:
        assert response.status_code == 409
        assert calls == []
    else:
        assert response.status_code == 200
        assert response.json()['status'] == 'refreshed'
        assert calls == [('refresh-executors', {'product_ids': [42], 'current_identities': [identity],
                                               'expected_source_revision': revision})]


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


def test_local_launch_publishes_new_executor_when_retained_order_is_fully_produced(db_session, monkeypatch):
    from app.services.production_control_journal import materialize_make_work_items
    from app.services.production_control_journal_projection import publish_local_make_changes

    work, identity, _revision = _current(db_session)
    current_run = db_session.get(models.PlanningRun, work.run_id)
    old = materialize_make_work_items(db_session, [work.id], launch_requests={
        work.id: {"launch_qty": 3, "expected_materialized_qty": 0},
    })["created"][0]
    old_product = db_session.get(models.ProductionProduct, old["product_id"])
    older_run = models.PlanningRun(status="SUPERSEDED", config_snapshot={}, source_plan_id=current_run.source_plan_id)
    db_session.add(older_run)
    db_session.flush()
    req = db_session.get(models.MrpRequirement, work.requirement_id)
    older_req = models.MrpRequirement(run_id=older_run.run_id, item_id=req.item_id,
        total_required_qty=3, net_required_qty=3, bom_level=0, freeze_version=1,
        period_from=req.period_from, period_to=req.period_to)
    db_session.add(older_req)
    db_session.flush()
    old_product.source_mrp_requirement_id = older_req.id
    old_product.order.source_run_id = older_run.run_id
    old_product.order.order_ref1c = "completed-retained-1c-order"
    old_product.produced_qty = 3
    old_product.remaining_qty = 0
    db_session.commit()

    def materials(db, ids, **kwargs):
        assert old_product.product_id not in ids
        return {pid: {"qty": float(db.get(models.ProductionProduct, pid).quantity),
                      "components": [], "coverage_status": "ready"} for pid in ids}
    monkeypatch.setattr("app.services.production_control_material_availability.preview_materials_bulk", materials)
    monkeypatch.setattr("app.services.production_control_journal_projection.build_route_sheet_snapshot_payloads",
        lambda db, product_ids, **kwargs: {pid: {"version": 1, "sheet": {}} for pid in product_ids})
    monkeypatch.setattr("app.services.production_control_material_availability.preview_make_work_item_materials",
        lambda *args, **kwargs: {"qty": kwargs["quantity"], "components": [], "coverage_status": "ready", "coverage_label": "Обеспечен"})
    result = materialize_make_work_items(db_session, [work.id], launch_requests={
        work.id: {"launch_qty": 3, "expected_materialized_qty": 0},
    }, _commit=False)
    new_pid = result["created"][0]["product_id"]
    publish_local_make_changes(db_session, [work.id])
    db_session.commit()
    rows = db_session.query(models.CurrentExecutionRow).filter_by(entity_kind="production_control_journal").all()
    assert any(row.payload.get("product_id") == new_pid for row in rows)
    assert not any(row.payload.get("product_id") == old_product.product_id for row in rows)
    assert next(row for row in rows if row.business_identity == identity).payload["launchable_qty"] == 5
    assert old_product.order.order_ref1c == "completed-retained-1c-order"
    assert old_product.order.order_state_key is None


def test_command_worker_does_not_race_physical_publication(monkeypatch):
    from app.services import production_control_command_worker as worker
    def busy(db, *, action):
        assert action == 'materials'
        raise HTTPException(409, detail={"code": "production_ledger_publication_busy"})
    monkeypatch.setattr(worker, '_lock_command_publication', busy)
    monkeypatch.setattr(worker, '_execute', lambda *a: pytest.fail('busy Ledger must not write'))
    with pytest.raises(HTTPException) as error:
        from types import SimpleNamespace
        worker.execute(SimpleNamespace(rollback=lambda: None), 'materials', {})
    assert error.value.status_code == 409


def test_command_worker_waits_briefly_before_reporting_busy_launch(monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy.exc import DBAPIError
    from app.services import production_control_command_worker as worker

    statements = []
    lock_calls = []
    lock_error = DBAPIError(
        'SELECT planning_truth_state FOR UPDATE', {},
        SimpleNamespace(pgcode='55P03'),
    )

    class Query:
        def filter_by(self, **values):
            assert values == {'id': 1}
            return self

        def with_for_update(self, **options):
            lock_calls.append(options)
            return self

        def one(self):
            raise lock_error

    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
        execute=lambda statement: statements.append(str(statement)),
        query=lambda _model: Query(),
    )

    with pytest.raises(HTTPException) as error:
        worker._lock_command_publication(db, action='materialize')

    assert statements == ["SET LOCAL lock_timeout = '3000ms'"]
    assert lock_calls == [{}]
    assert error.value.status_code == 409
    assert error.value.detail == {
        'code': 'production_ledger_publication_busy',
        'message': (
            'Идёт публикация нового состояния журнала. Запуск не выполнен, '
            'исполнительный заказ не создан. Повторите запуск после обновления журнала.'
        ),
    }


def test_command_worker_restores_default_lock_timeout_after_acquiring_boundary():
    from types import SimpleNamespace
    from app.services import production_control_command_worker as worker

    statements = []

    class Query:
        def filter_by(self, **values):
            assert values == {'id': 1}
            return self

        def with_for_update(self, **options):
            assert options == {}
            return self

        def one(self):
            return object()

    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
        execute=lambda statement: statements.append(str(statement)),
        query=lambda _model: Query(),
    )

    worker._lock_command_publication(db, action='materials')

    assert statements == [
        "SET LOCAL lock_timeout = '3000ms'",
        "SET LOCAL lock_timeout = DEFAULT",
    ]


def test_command_worker_rolls_back_failed_publication_transaction(monkeypatch):
    from types import SimpleNamespace
    from app.services import production_control_command_worker as worker
    calls = []
    monkeypatch.setattr(worker, '_lock_command_publication', lambda db, *, action: calls.append(f'lock:{action}'))
    def fail(*args):
        raise ValueError('failed command')
    monkeypatch.setattr(worker, '_execute', fail)
    with pytest.raises(ValueError):
        worker.execute(SimpleNamespace(rollback=lambda: calls.append('rollback')), 'materialize', {})
    assert calls == ['lock:materialize', 'rollback']


def test_current_command_runs_while_physical_import_lifecycle_is_busy(db_session, monkeypatch):
    from app.services import production_control_command_worker as worker
    from app.services.item_ledger import physical_refresh_orchestrator as physical
    _scope(db_session)
    monkeypatch.setattr(physical, '_acquire_lifecycle_lock', lambda db: pytest.fail('import mutex must not guard current commands'))
    monkeypatch.setattr(worker, '_execute', lambda *a: {'status': 'ok'})
    assert worker.execute(db_session, 'refresh-executors', {}) == {'status': 'ok'}


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


def test_executor_publication_refreshes_1c_number_and_print_without_proposal(db_session, monkeypatch):
    from app.services.production_control_journal import materialize_make_work_items
    from app.services.production_control_journal_projection import publish_current_executor_changes, _candidate_business_identity
    work, req, owner = _scope(db_session)
    db_session.get(models.Item,work.item_id).optimal_batch=8
    db_session.flush()
    created=materialize_make_work_items(db_session,[work.id],launch_requests={work.id:{'launch_qty':8,'expected_materialized_qty':0}})['created'][0]
    product=db_session.get(models.ProductionProduct,created['product_id']);order=product.order
    original={'product_id':product.product_id,'order_id':order.order_id,'item_id':product.item_id,
              'source_mrp_requirement_id':req.id,'source_mrp_allocation_key':product.source_mrp_allocation_key,
              'line_number':1,'quantity':8,'spec_id':product.spec_id,'spec_revision_hash':product.spec_revision_hash,'order_ref1c':None,'order_one_c_number':None,'root_item_ids':[product.item_id],
              'material_coverage_snapshot':{'qty':8,'components':[],'coverage_status':'ready'},'_route_sheet_snapshot':{'version':1,'sheet':{'one_c_number':''}}}
    identity=_candidate_business_identity(original)
    original['current_identity']=identity
    publish_current_production_control_from_payload(db_session,work.ledger_generation_id,{'meta':{},'rows':[original]})
    # The external write is already durable; no proposal remains after a full launch.
    order.order_ref1c='created-1c-ref'
    db_session.add(models.SyncLink(source_system='PRODPLAN',target_system='1C',source_doctype='production_order',source_id=order.order_id,
        target_entity='Document_ЗаказНаПроизводство',target_ref_key=order.order_ref1c,target_number='PP059717709',status='success'))
    issue=models.ProductionMaterialIssue(product_id=product.product_id,order_id=order.order_id,document_number='LOCAL-TRANSFER',
        status='exported',source_warehouse_ref1c='source',warehouse_ref1c='destination',ledger_generation_id=work.ledger_generation_id)
    db_session.add(issue);db_session.flush()
    db_session.add(models.SyncLink(source_system='PRODPLAN',target_system='1C',source_doctype='material_issue',source_id=issue.issue_id,
        target_entity='Document_ПеремещениеЗапасов',target_ref_key='transfer-ref',target_number='MT059717709',status='success'))
    db_session.commit()
    before=(owner.reserved_qty,owner.covered_from_stock_at_freeze_qty,owner.replenishment_required_qty,owner.replenishment_received_qty)
    monkeypatch.setattr('app.services.production_control_material_availability.preview_materials_bulk',
                        lambda *a,**kw:pytest.fail('operational export must preserve accepted material math'))
    # Journal and printing use their real builders; they must pick up the 1C link.
    result=publish_current_executor_changes(db_session,[product.product_id])
    db_session.commit()
    record=db_session.query(models.CurrentExecutionRow).filter_by(entity_kind='production_control_journal',business_identity=identity).one()
    assert record.payload['order_one_c_number']=='PP059717709'
    assert record.payload['order_ref1c']=='created-1c-ref'
    assert record.payload['_route_sheet_snapshot']['sheet']['one_c_number']=='PP059717709'
    assert 'MT059717709' in record.payload['_route_sheet_snapshot']['sheet']['transfer_rows'][0]['transfer_number']
    assert before==(owner.reserved_qty,owner.covered_from_stock_at_freeze_qty,owner.replenishment_required_qty,owner.replenishment_received_qty)
    assert result['current_identities']==[identity]
    again=publish_current_executor_changes(db_session,[product.product_id],current_identities=[identity],expected_source_revision=result['source_revision'])
    assert again['source_revision']==result['source_revision']
    with pytest.raises(HTTPException) as error:
        publish_current_executor_changes(db_session,[product.product_id],expected_source_revision='stale')
    assert error.value.status_code==409


def test_export_schedules_metadata_refresh_only_after_success_commit(monkeypatch):
    from types import SimpleNamespace
    from app.routers.production_control import (
        _refresh_exported_executors,
        _refresh_exported_executors_metadata,
    )
    from app.services import production_control_command_worker
    calls=[]

    def worker(*args):
        calls.append(("run_command", args))

    monkeypatch.setattr(production_control_command_worker, 'run_command', worker)

    class Tasks:
        def add_task(self, function, *args, **kwargs):
            assert calls == ['committed']
            calls.append(("scheduled", function, args, kwargs))

    receipt = {
        'status': 'ok',
        'issues_created': 1,
        'entries': [{'status': 'created', 'number': 'PP059717709'}],
    }
    result = _refresh_exported_executors(
        SimpleNamespace(commit=lambda: calls.append('committed')),
        [19487],
        receipt,
        background_tasks=Tasks(),
    )

    assert result is receipt
    assert result['entries'] == [{'status': 'created', 'number': 'PP059717709'}]
    assert result['projection_refresh_error'] == 'Обновление журнала выполняется в фоне'
    assert len(calls) == 2 and calls[0] == 'committed'
    _, function, args, kwargs = calls[1]
    assert function is _refresh_exported_executors_metadata
    assert args == ([19487],) and kwargs == {}
    assert not any(isinstance(call, tuple) and call[0] == 'run_command' for call in calls)

    function(*args, **kwargs)
    assert calls[-1] == (
        'run_command',
        ('refresh-executors', {'product_ids': [19487]}),
    )


def test_background_metadata_busy_cannot_change_success_receipt(monkeypatch):
    from app.routers.production_control import _refresh_exported_executors_metadata
    from app.services import production_control_command_worker

    receipt = {
        'status': 'ok',
        'entries': [{'status': 'existing', 'number': 'MT059717709'}],
    }
    calls = []

    def busy(*args):
        calls.append(args)
        raise HTTPException(409, detail={'message': 'Ledger refresh busy'})

    monkeypatch.setattr(production_control_command_worker, 'run_command', busy)

    assert _refresh_exported_executors_metadata([19487, 19488]) is None
    assert calls == [
        ('refresh-executors', {'product_ids': [19487, 19488]})
    ]
    assert receipt == {
        'status': 'ok',
        'entries': [{'status': 'existing', 'number': 'MT059717709'}],
    }


def test_asgi_sends_success_receipt_before_metadata_background_starts(monkeypatch):
    import asyncio
    import json
    from fastapi import BackgroundTasks, FastAPI
    from app.routers import production_control

    timeline = []

    def metadata_only(product_ids):
        timeline.append(("background", list(product_ids)))

    monkeypatch.setattr(
        production_control,
        "_refresh_exported_executors_metadata",
        metadata_only,
    )

    app = FastAPI()

    @app.post("/export")
    def export(background_tasks: BackgroundTasks):
        receipt = {
            "status": "ok",
            "entries": [{"status": "created", "number": "PP059717709"}],
        }
        return production_control._refresh_exported_executors(
            type("DB", (), {"commit": lambda self: timeline.append("committed")})(),
            [19487],
            receipt,
            background_tasks=background_tasks,
        )

    sent = []
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body":
            assert not any(
                isinstance(entry, tuple) and entry[0] == "background"
                for entry in timeline
            )
            timeline.append("body-sent")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/export",
        "raw_path": b"/export",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))

    body = next(message for message in sent if message["type"] == "http.response.body")
    payload = json.loads(body["body"])
    assert payload["status"] == "ok"
    assert payload["entries"] == [
        {"status": "created", "number": "PP059717709"}
    ]
    assert timeline == ["committed", "body-sent", ("background", [19487])]
