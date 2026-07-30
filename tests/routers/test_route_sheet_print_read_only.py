"""Route-sheet HTTP method semantics."""

from app.routers import production_control as routes


def test_get_route_sheet_is_read_only_even_with_legacy_mark_flag(monkeypatch):
    marked: list[list[int]] = []
    monkeypatch.setattr(
        routes,
        "render_route_sheets_html",
        lambda _db, ids, *, auto_print: f"<html>{ids}:{auto_print}</html>",
    )
    monkeypatch.setattr(
        routes,
        "mark_route_sheets_printed",
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
    monkeypatch.setattr(
        routes,
        "render_route_sheets_html",
        lambda _db, ids, *, auto_print: f"<html>{ids}:{auto_print}</html>",
    )
    monkeypatch.setattr(
        routes,
        "mark_route_sheets_printed",
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
    assert marked == [[8, 9]]
