"""Route-sheet current execution contract and HTTP method semantics."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import production_control as routes


def _manifest():
    return SimpleNamespace(source_revision="accepted:g7", source_generation_id=7)


def _row(product_id: int, *, identity: str | None = None, chain: dict | None = None):
    return SimpleNamespace(
        business_identity=identity or f"production:order:{product_id}",
        payload={
            "product_id": product_id,
            "route_sheet_payload": {
                "version": 1,
                "anchor_product_id": product_id,
                "sheet": {
                    "product_id": product_id,
                    "remaining_qty": 1,
                    "components": [],
                    "chain": chain or {},
                    "operations": [],
                    "weld_operations": [],
                    "transfer_rows": [],
                    "route_context": {},
                },
            },
        },
    )


def _patch_current(monkeypatch, rows):
    monkeypatch.setattr(routes, "require_current_execution_scope", lambda *args, **kwargs: _manifest())
    monkeypatch.setattr(routes, "load_current_execution_rows", lambda *args, **kwargs: list(rows))


def test_get_route_sheet_is_read_only_even_with_legacy_mark_flag(monkeypatch):
    marked: list[list[int]] = []
    row = _row(7)
    _patch_current(monkeypatch, [row])
    monkeypatch.setattr(routes, "render_route_sheets_from_snapshots", lambda payloads, **kwargs: "<html>current</html>")
    monkeypatch.setattr(routes, "mark_route_sheets_printed_by_members", lambda *args: marked.append(list(args[1])))

    response = routes.print_route_sheets(
        product_ids="7",
        current_identities="production:order:7",
        expected_source_revision="accepted:g7",
        mark_printed=True,
        auto_print=False,
        db=object(),
    )

    assert response.status_code == 200
    assert marked == []


def test_post_route_sheet_marks_current_sheet_members_only(monkeypatch):
    marked: list[list[int]] = []
    row = _row(7, chain={"weld_product_id": 8, "weld_qty": 1})
    _patch_current(monkeypatch, [row])
    monkeypatch.setattr(routes, "render_route_sheets_from_snapshots", lambda *args, **kwargs: "<html>current</html>")
    monkeypatch.setattr(routes, "mark_route_sheets_printed_by_members", lambda *args: marked.append(list(args[1])))

    routes.post_print_route_sheets(
        payload=routes.PrintRouteSheetsPayload(
            product_ids=[7],
            current_identities=["production:order:7"],
            expected_source_revision="accepted:g7",
            mark_printed=True,
            auto_print=False,
        ),
        db=object(),
    )

    assert marked == [[7, 8]]


def test_route_sheets_missing_current_manifest_is_503_even_with_legacy_rows(monkeypatch):
    from app.services.item_ledger.current_execution import CurrentExecutionUnavailable

    monkeypatch.setattr(
        routes,
        "require_current_execution_scope",
        lambda *args, **kwargs: (_ for _ in ()).throw(CurrentExecutionUnavailable("current manifest missing")),
    )

    with pytest.raises(HTTPException) as exc_info:
        routes.print_route_sheets(
            product_ids="7",
            current_identities="production:order:7",
            expected_source_revision="accepted:g7",
            db=object(),
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "production_control_current_unavailable"
