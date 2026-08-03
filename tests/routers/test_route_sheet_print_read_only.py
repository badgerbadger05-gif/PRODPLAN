"""Route-sheet HTTP method semantics."""

import pytest
from fastapi import HTTPException

from app.routers import production_control as routes
from app.services.production_control_journal_snapshot import RouteSheetSnapshotUnavailable


def test_get_route_sheet_is_read_only_even_with_legacy_mark_flag(monkeypatch):
    marked: list[list[int]] = []
    snapshots = [
        {"version": 1, "anchor_product_id": 7, "sheet": {"product_id": 7, "remaining_qty": 1, "components": [], "chain": {}, "operations": [], "weld_operations": [], "transfer_rows": [], "route_context": {}}},
        {"version": 1, "anchor_product_id": 8, "sheet": {"product_id": 8, "remaining_qty": 2, "components": [], "chain": {}, "operations": [], "weld_operations": [], "transfer_rows": [], "route_context": {}}},
    ]
    monkeypatch.setattr(
        routes,
        "read_route_sheet_snapshot_rows",
        lambda _db, ids: snapshots,
    )
    monkeypatch.setattr(
        routes,
        "render_route_sheets_from_snapshots",
        lambda payloads, *, auto_print: f"<html>{payloads}:{auto_print}</html>",
    )
    monkeypatch.setattr(
        routes,
        "mark_route_sheets_printed_by_snapshot_members",
        lambda _db, ids: marked.append(list(ids)),
    )

    response = routes.print_route_sheets(
        product_ids="7,8",
        mark_printed=True,
        auto_print=False,
        db=object(),
    )

    assert response.status_code == 200
    assert marked == []


def test_post_route_sheet_marks_only_when_explicitly_requested(monkeypatch):
    marked: list[list[int]] = []
    snapshots = [
        {"version": 1, "anchor_product_id": 7, "sheet": {"product_id": 7, "remaining_qty": 1, "components": [], "chain": {}, "operations": [], "weld_operations": [], "transfer_rows": [], "route_context": {}}},
        {"version": 1, "anchor_product_id": 9, "sheet": {"product_id": 9, "remaining_qty": 3, "components": [], "chain": {}, "operations": [], "weld_operations": [], "transfer_rows": [], "route_context": {}}},
    ]
    monkeypatch.setattr(
        routes,
        "read_route_sheet_snapshot_rows",
        lambda _db, ids: snapshots,
    )
    monkeypatch.setattr(
        routes,
        "render_route_sheets_from_snapshots",
        lambda payloads, *, auto_print: f"<html>{payloads}:{auto_print}</html>",
    )
    monkeypatch.setattr(
        routes,
        "mark_route_sheets_printed_by_snapshot_members",
        lambda _db, ids: marked.append(list(ids)),
    )

    routes.post_print_route_sheets(
        payload=routes.PrintRouteSheetsPayload(
            product_ids=[7],
            mark_printed=False,
            auto_print=False,
        ),
        db=object(),
    )
    assert marked == []

    routes.post_print_route_sheets(
        payload=routes.PrintRouteSheetsPayload(
            product_ids=[8, 9],
            mark_printed=True,
            auto_print=False,
        ),
        db=object(),
    )
    assert marked == [[7, 9]]


def test_post_route_sheet_marks_persisted_chain_members(monkeypatch):
    marked: list[list[int]] = []
    snapshot = {
        "version": 1,
        "anchor_product_id": 7,
        "sheet": {
            "product_id": 7,
            "remaining_qty": 1,
            "components": [],
            "operations": [],
            "weld_operations": [],
            "transfer_rows": [],
            "route_context": {},
            "chain": {"weld_product_id": 8, "weld_qty": 1},
        },
    }
    monkeypatch.setattr(routes, "read_route_sheet_snapshot_rows", lambda _db, _ids: [snapshot])
    monkeypatch.setattr(routes, "render_route_sheets_from_snapshots", lambda _rows, *, auto_print: "<html></html>")
    monkeypatch.setattr(routes, "mark_route_sheets_printed_by_snapshot_members", lambda _db, ids: marked.append(list(ids)))

    routes.post_print_route_sheets(
        payload=routes.PrintRouteSheetsPayload(product_ids=[8], mark_printed=True),
        db=object(),
    )

    assert marked == [[7, 8]]


def test_route_sheets_print_fails_closed_without_snapshot(monkeypatch):
    monkeypatch.setattr(
        routes,
        "read_route_sheet_snapshot_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RouteSheetSnapshotUnavailable({"reason": "route snapshot is missing"})
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        routes.print_route_sheets(
            product_ids="7",
            mark_printed=False,
            auto_print=False,
            db=object(),
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "route_sheet_snapshot_unavailable"

    with pytest.raises(HTTPException) as post_exc:
        routes.post_print_route_sheets(
            payload=routes.PrintRouteSheetsPayload(
                product_ids=[7],
                mark_printed=False,
                auto_print=False,
            ),
            db=object(),
        )
    assert post_exc.value.status_code == 503
    assert post_exc.value.detail["code"] == "route_sheet_snapshot_unavailable"
