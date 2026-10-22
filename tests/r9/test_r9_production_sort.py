from backend.app.routers.production_control import (
    _canonical_journal_sort,
    _group_journal_rows_by_article,
)


def _row(order_number, order_date, line_number):
    return {
        "order_number": order_number,
        "order_date": order_date,
        "line_number": line_number,
    }


def test_default_journal_sort_puts_non_null_dates_desc_and_ties_asc():
    rows = [
        _row("B", None, 1),
        _row("A", "2026-09-10", 2),
        _row("A", "2026-09-10", 1),
        _row("C", "2026-09-09", 1),
    ]

    _canonical_journal_sort(rows, field=None, descending=True)

    assert [(r["order_date"], r["order_number"], r["line_number"]) for r in rows] == [
        ("2026-09-10", "A", 1),
        ("2026-09-10", "A", 2),
        ("2026-09-09", "C", 1),
        (None, "B", 1),
    ]


def test_explicit_descending_sort_keeps_ties_ascending_and_nulls_last():
    rows = [
        _row("B", None, 1),
        _row("A", "2026-09-09", 2),
        _row("A", "2026-09-09", 1),
        _row("C", "2026-09-10", 1),
    ]

    _canonical_journal_sort(rows, field="order_date", descending=True)

    assert [(r["order_date"], r["order_number"], r["line_number"]) for r in rows] == [
        ("2026-09-10", "C", 1),
        ("2026-09-09", "A", 1),
        ("2026-09-09", "A", 2),
        (None, "B", 1),
    ]


def _item_row(order_number, planned_start, item_id, item_article):
    return {
        "order_number": order_number,
        "planned_start_date": planned_start,
        "item_id": item_id,
        "item_article": item_article,
        "line_number": 1,
    }


def test_article_grouping_pulls_same_item_rows_into_one_block():
    rows = [
        _item_row("MRP-R-1", "2026-10-01", 3779, "CA-007420-SP"),
        _item_row("MRP-R-2", "2026-10-02", 4000, "CA-000636-S"),
        _item_row("MRP-R-3", "2026-10-03", 3779, "CA-007420-SP"),
        _item_row("MRP-R-4", "2026-10-04", 3779, "CA-007420-SP"),
        _item_row("MRP-R-5", "2026-10-05", 5000, "CA-000910-SP"),
    ]

    _canonical_journal_sort(rows, field="planned_start_date", descending=False)
    _group_journal_rows_by_article(rows)

    assert [r["order_number"] for r in rows] == [
        "MRP-R-1",
        "MRP-R-3",
        "MRP-R-4",
        "MRP-R-2",
        "MRP-R-5",
    ]


def test_article_grouping_keeps_sorted_order_inside_block():
    rows = [
        _item_row("MRP-R-1", "2026-10-01", 3779, "CA-007420-SP"),
        _item_row("MRP-R-2", "2026-10-02", 4000, "CA-000636-S"),
        _item_row("MRP-R-3", "2026-10-05", 3779, "CA-007420-SP"),
        _item_row("MRP-R-4", "2026-10-03", 3779, "CA-007420-SP"),
    ]

    _canonical_journal_sort(rows, field="planned_start_date", descending=False)
    _group_journal_rows_by_article(rows)

    assert [(r["order_number"], r["planned_start_date"]) for r in rows] == [
        ("MRP-R-1", "2026-10-01"),
        ("MRP-R-4", "2026-10-03"),
        ("MRP-R-3", "2026-10-05"),
        ("MRP-R-2", "2026-10-02"),
    ]


def test_article_grouping_falls_back_to_article_when_item_id_missing():
    rows = [
        _item_row("MRP-R-1", "2026-10-01", None, "CA-007420-SP"),
        _item_row("MRP-R-2", "2026-10-02", 4000, "CA-000636-S"),
        _item_row("MRP-R-3", "2026-10-03", None, "CA-007420-SP"),
    ]

    _group_journal_rows_by_article(rows)

    assert [r["order_number"] for r in rows] == ["MRP-R-1", "MRP-R-3", "MRP-R-2"]
