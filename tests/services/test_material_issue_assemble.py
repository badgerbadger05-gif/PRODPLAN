"""Кнопка «Собрано»: проведение перемещения ставит recorder pull в Item Ledger.

`_mark_issue_exported` (one_c_stock_transfer_export) ставит pull при создании
НЕпроведённого документа. Реальное складское движение появляется только после
`Unpost` + `Post?PostingModeOperational=true`, который выполняет
`assemble_material_issue` — значит именно он обязан поставить пул повторно,
иначе движение доезжает до Ledger только реконсайл-свипом.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app import models
from app.services import planning_truth
from app.services import one_c_stock_transfer_export as transfer_export
from app.services import production_control_material_issues as issues

TRANSFER_ENTITY = "Document_ПеремещениеЗапасов"


class _FakeClient:
    def __init__(self) -> None:
        self.operations: list = []
        self.patches: list = []

    def patch(self, entity_ref, payload, **_kwargs):
        self.patches.append((entity_ref, payload))
        return {}

    def post_operation(self, operation_path):
        self.operations.append(operation_path)


def _accepted(db, key="assemble"):
    cutoff = datetime(2026, 7, 23, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key=f"mi-{key}", status="completed", cutoff=cutoff, source_watermarks={}
    )
    generation = models.LedgerGeneration(
        generation_key=f"mi-{key}",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        physical_import_batch=batch,
        source_watermarks={},
        capabilities={},
        algorithm_version="test",
    )
    db.add_all((batch, generation))
    db.flush()
    planning_truth.publish_generation(db, generation)
    return generation


def _issue(db, generation, *, ref1c: str = "transfer-ref-1"):
    order = db.get(models.ProductionOrder, 1)
    if order is None:
        order = models.ProductionOrder(
            order_id=1,
            order_number="MRP-ASSEMBLE",
            order_date=datetime(2026, 7, 23, tzinfo=timezone.utc),
            source="mrp",
        )
        db.add(order)
        db.flush()
    issue = models.ProductionMaterialIssue(
        document_number="MI-ASSEMBLE",
        product_id=1,
        order_id=1,
        ledger_generation_id=generation.id,
        status="exported",
        direction="issue",
        exported_ref1c=ref1c,
    )
    db.add(issue)
    db.commit()
    return issue


def _stub_1c(monkeypatch, fake):
    monkeypatch.setattr(
        issues,
        "_load_odata_config",
        lambda: {"base_url": "http://mtzw7/unf_demo/odata/standard.odata"},
    )
    monkeypatch.setattr(issues, "_create_odata_client", lambda *a, **kw: fake)
    # Полный payload перемещения здесь не важен: проверяем проведение и pull.
    monkeypatch.setattr(
        transfer_export, "_collect_export_entries", lambda db, ids: ([], [])
    )


def test_assemble_posts_operationally_and_enqueues_recorder_pull(db_session, monkeypatch):
    generation = _accepted(db_session)
    issue = _issue(db_session, generation)
    fake = _FakeClient()
    _stub_1c(monkeypatch, fake)

    result = issues.assemble_material_issue(
        db_session, issue.issue_id
    )

    assert result["status"] == "ok"
    assert fake.operations == [
        f"{TRANSFER_ENTITY}(guid'transfer-ref-1')/Unpost",
        f"{TRANSFER_ENTITY}(guid'transfer-ref-1')/Post?PostingModeOperational=true",
    ]

    pull = (
        db_session.query(models.StockRecorderPull)
        .filter(models.StockRecorderPull.recorder_ref == "transfer-ref-1")
        .one()
    )
    assert pull.recorder_type == TRANSFER_ENTITY
    assert pull.status == "pending"
    assert pull.source == "material_issue_assemble"


def test_assemble_repull_resets_an_exhausted_pull_row(db_session, monkeypatch):
    """Ранее выгруженный (и провалившийся) pull возвращается в очередь."""
    generation = _accepted(db_session)
    issue = _issue(db_session, generation)
    db_session.add(
        models.StockRecorderPull(
            recorder_type=TRANSFER_ENTITY,
            recorder_ref="transfer-ref-1",
            status="error",
            attempts=5,
            line_count=0,
            source="stock_transfer_export",
        )
    )
    db_session.commit()
    fake = _FakeClient()
    _stub_1c(monkeypatch, fake)

    issues.assemble_material_issue(db_session, issue.issue_id)

    rows = (
        db_session.query(models.StockRecorderPull)
        .filter(models.StockRecorderPull.recorder_ref == "transfer-ref-1")
        .all()
    )
    assert len(rows) == 1  # без дублей: get-or-create по (type, ref)
    assert rows[0].status == "pending"


def test_assemble_accepts_stale_executor_when_parent_order_is_current(
    db_session, monkeypatch
):
    creation_generation = _accepted(db_session, key="stale-creation")
    issue = _issue(db_session, creation_generation)
    current_generation = _accepted(db_session, key="stale-current")
    fake = _FakeClient()
    _stub_1c(monkeypatch, fake)
    checked: list[tuple[list[int], str]] = []

    def _require_current_parent(db, orders, *, consumer):
        checked.append(([int(order.order_id) for order in orders], consumer))
        return int(current_generation.id)

    monkeypatch.setattr(
        issues, "require_materialized_orders", _require_current_parent
    )

    result = issues.assemble_material_issue(db_session, issue.issue_id)

    assert result["status"] == "ok"
    assert checked == [
        ([1], "production_material_issue_assemble_stale_executor")
    ]
    assert fake.operations[-1].endswith("/Post?PostingModeOperational=true")


def test_assemble_rejects_stale_executor_when_parent_order_is_not_current(
    db_session, monkeypatch
):
    creation_generation = _accepted(db_session, key="retired-creation")
    issue = _issue(db_session, creation_generation)
    _accepted(db_session, key="retired-current")
    fake = _FakeClient()
    _stub_1c(monkeypatch, fake)

    def _reject_parent(*_args, **_kwargs):
        raise ValueError("parent MRP obligation is retired")

    monkeypatch.setattr(issues, "require_materialized_orders", _reject_parent)

    try:
        issues.assemble_material_issue(db_session, issue.issue_id)
        assert False, "ожидали ValueError"
    except ValueError as exc:
        assert "retired" in str(exc)

    assert fake.operations == []


def test_assemble_failure_does_not_enqueue_pull(db_session, monkeypatch):
    generation = _accepted(db_session)
    issue = _issue(db_session, generation)

    class _FailingClient(_FakeClient):
        def post_operation(self, operation_path):
            self.operations.append(operation_path)
            raise RuntimeError("1С отказала в проведении")

    fake = _FailingClient()
    _stub_1c(monkeypatch, fake)

    try:
        issues.assemble_material_issue(db_session, issue.issue_id)
        assert False, "ожидали ValueError"
    except ValueError as exc:
        assert "отказала" in str(exc)

    assert db_session.query(models.StockRecorderPull).count() == 0
    db_session.refresh(issue)
    assert issue.status != "posted"
