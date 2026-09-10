from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import models
from app.routers.production_control import (
    MaterialIssueCreatePayload,
    PrintRouteSheetsPayload,
    get_work_item_materials,
    post_open_paint_weld_chains,
    post_print_route_sheets,
    print_route_sheets,
    post_material_issues,
)


class _FakeDb:
    def __init__(self, *, work_id: int = 702):
        self.generation = SimpleNamespace(cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc))
        self.work = SimpleNamespace(
            id=work_id,
            ledger_generation_id=7,
            requirement_id=900,
            item_id=10,
            replenishment_remaining_qty=5,
            run_id=1,
        )

    def get(self, model, identifier):
        if model is models.LedgerGeneration:
            return self.generation if int(identifier) == 7 else None
        if model is models.ReplenishmentWorkItem:
            return self.work if int(identifier) == int(self.work.id) else None
        return None


def _manifest():
    return SimpleNamespace(source_revision="accepted:g7", source_generation_id=7)


def _current_row(identity="production-mrp-requirement:900:alloc:A", product_id=77):
    return SimpleNamespace(
        id=1,
        business_identity=identity,
        payload={
            "source_mrp_requirement_id": 900,
            "item_id": 10,
            "product_id": product_id,
            "route_sheet_payload": {
                "version": 1,
                "anchor_product_id": product_id,
                "sheet": {"product_id": product_id, "order_number": "ORD-77", "components": [], "operations": []},
            },
            "material_coverage_snapshot": {
                "ledger_generation_id": 7,
                "line_quantity": 5,
                "components": [{"item_id": 20, "required_qty": 2}],
            },
        },
    )


def test_work_item_materials_uses_current_identity_and_never_replays(monkeypatch):
    import app.routers.production_control as router
    import app.services.item_ledger.current_execution as current_execution

    db = _FakeDb(work_id=702)
    monkeypatch.setattr(current_execution, "require_current_execution_scope", lambda *args, **kwargs: _manifest())
    monkeypatch.setattr(current_execution, "load_current_execution_rows", lambda *args, **kwargs: [_current_row()])
    monkeypatch.setattr(router, "preview_make_work_item_materials", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("GET replayed material coverage")))

    with pytest.raises(Exception) as stale:
        get_work_item_materials(
            701,
            qty=5,
            ledger_generation_id=7,
            current_identity="production-mrp-requirement:900:alloc:A",
            expected_source_revision="accepted:g7",
            db=db,
        )
    assert getattr(stale.value, "status_code", None) == 503

    result = get_work_item_materials(
        702,
        qty=5,
        ledger_generation_id=7,
        current_identity="production-mrp-requirement:900:alloc:A",
        expected_source_revision="accepted:g7",
        db=db,
    )
    assert result["components"][0]["item_id"] == 20

    with pytest.raises(Exception) as wrong_qty:
        get_work_item_materials(
            702,
            qty=4,
            ledger_generation_id=7,
            current_identity="production-mrp-requirement:900:alloc:A",
            expected_source_revision="accepted:g7",
            db=db,
        )
    assert getattr(wrong_qty.value, "status_code", None) == 503


def test_open_paint_weld_requires_current_rows_and_revision(monkeypatch):
    import app.routers.production_control as router
    called = []
    monkeypatch.setattr(router, "open_paint_chains_for_products", lambda *args, **kwargs: called.append(True))

    with pytest.raises(Exception) as caught:
        post_open_paint_weld_chains(
            router.OpenPaintWeldChainsPayload(product_ids=[77], initiated_by="test"),
            db=object(),
        )
    assert getattr(caught.value, "status_code", None) == 409
    assert called == []


def test_open_paint_weld_fails_closed_when_counterpart_lacks_current_anchor(monkeypatch):
    import app.routers.production_control as router

    row = _current_row(product_id=77)
    row.payload.update({"item_id": 10, "source_run_id": 5, "source_mrp_requirement_id": 11})
    db = _AnchorDb(requirements=[SimpleNamespace(id=101, run_id=5, item_id=20, freeze_version=1)])
    monkeypatch.setattr(router, "require_current_execution_scope", lambda *args, **kwargs: _manifest())
    monkeypatch.setattr(router, "load_current_execution_rows", lambda *args, **kwargs: [row])
    monkeypatch.setattr(
        router,
        "open_paint_chains_for_products",
        lambda *args, **kwargs: {"status": "opened", "product_ids": [77, 88]},
    )

    with pytest.raises(Exception) as caught:
        post_open_paint_weld_chains(
            router.OpenPaintWeldChainsPayload(
                product_ids=[77],
                current_identities=["production-mrp-requirement:900:alloc:A"],
                expected_source_revision="accepted:g7",
                initiated_by="test",
            ),
            db=db,
        )
    assert getattr(caught.value, "status_code", None) == 503


class _ModelQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _AnchorDb:
    def __init__(self, *, requirements=(), product=None):
        self.requirements = list(requirements)
        self.product = product

    def query(self, model):
        if model is models.PaintWeldPair:
            return _ModelQuery([SimpleNamespace(painted_item_id=10, welded_item_id=20, is_active=True)])
        if model is models.MrpRequirement:
            return _ModelQuery(self.requirements)
        return _ModelQuery([])

    def get(self, model, identifier):
        if model is models.ProductionProduct and self.product is not None:
            return self.product if int(identifier) == int(self.product.product_id) else None
        return None


def test_open_paint_rejects_foreign_requirement_before_committing_chain(monkeypatch):
    import app.routers.production_control as router

    source = _current_row(product_id=77)
    source.payload.update({"item_id": 10, "source_run_id": 5, "source_mrp_requirement_id": 11})
    foreign = _current_row(identity="production-mrp-requirement:999:alloc:X", product_id=0)
    foreign.payload.update({"item_id": 20, "source_run_id": 5, "source_mrp_requirement_id": 999})
    requirement_a = SimpleNamespace(id=101, run_id=5, item_id=20, freeze_version=1)
    db = _AnchorDb(requirements=[requirement_a])
    monkeypatch.setattr(router, "require_current_execution_scope", lambda *args, **kwargs: _manifest())
    monkeypatch.setattr(router, "load_current_execution_rows", lambda *args, **kwargs: [source, foreign])
    called = []
    monkeypatch.setattr(router, "open_paint_chains_for_products", lambda *args, **kwargs: called.append(True))

    with pytest.raises(Exception) as caught:
        post_open_paint_weld_chains(
            router.OpenPaintWeldChainsPayload(
                product_ids=[77],
                current_identities=["production-mrp-requirement:900:alloc:A"],
                expected_source_revision="accepted:g7",
            ),
            db=db,
        )
    assert getattr(caught.value, "status_code", None) == 503
    assert called == []


def test_material_issue_accepts_exact_proposal_anchor_for_new_product(monkeypatch):
    import app.routers.production_control as router

    identity = "production-mrp-requirement:101:alloc:A"
    proposal = _current_row(identity=identity, product_id=0)
    proposal.payload.update({"item_id": 20, "source_mrp_requirement_id": 101, "source_run_id": 5})
    product = SimpleNamespace(product_id=88, item_id=20, source_mrp_requirement_id=101)
    db = _AnchorDb(product=product)
    monkeypatch.setattr(router, "require_current_execution_scope", lambda *args, **kwargs: _manifest())
    monkeypatch.setattr(router, "load_current_execution_rows", lambda *args, **kwargs: [proposal])
    monkeypatch.setattr(router, "create_material_issues", lambda *args, **kwargs: {"status": "ok"})

    result = post_material_issues(
        MaterialIssueCreatePayload(
            product_ids=[88],
            current_identities=[identity],
            expected_source_revision="accepted:g7",
        ),
        db=db,
    )
    assert result["status"] == "ok"


def test_route_sheet_get_and_post_use_current_payload_not_snapshot_reader(monkeypatch):
    import app.routers.production_control as router
    import app.services.item_ledger.current_execution as current_execution

    db = object()
    row = _current_row(product_id=77)
    monkeypatch.setattr(router, "require_current_execution_scope", lambda *args, **kwargs: _manifest())
    monkeypatch.setattr(router, "load_current_execution_rows", lambda *args, **kwargs: [row])
    monkeypatch.setattr(router, "render_route_sheets_from_snapshots", lambda payloads, **kwargs: "<html>current</html>")
    printed = []
    monkeypatch.setattr(router, "mark_route_sheets_printed_by_members", lambda *args, **kwargs: printed.append(True))

    html = print_route_sheets(
        product_ids="77",
        current_identities="production-mrp-requirement:900:alloc:A",
        expected_source_revision="accepted:g7",
        db=db,
    )
    assert "current" in html.body.decode()
    posted = post_print_route_sheets(
        PrintRouteSheetsPayload(
            product_ids=[77],
            current_identities=["production-mrp-requirement:900:alloc:A"],
            expected_source_revision="accepted:g7",
            mark_printed=True,
        ),
        db=db,
    )
    assert "current" in posted.body.decode()
    assert printed == [True]


def test_route_sheet_rejects_foreign_current_identity(monkeypatch):
    import app.routers.production_control as router

    monkeypatch.setattr(router, "require_current_execution_scope", lambda *args, **kwargs: _manifest())
    monkeypatch.setattr(router, "load_current_execution_rows", lambda *args, **kwargs: [_current_row()])
    with pytest.raises(Exception) as caught:
        print_route_sheets(
            product_ids="77",
            current_identities="production-mrp-requirement:other:alloc:X",
            expected_source_revision="accepted:g7",
            db=object(),
        )
    assert getattr(caught.value, "status_code", None) == 503
