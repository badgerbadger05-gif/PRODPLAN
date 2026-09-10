from backend.app.routers.production_control import _canonical_journal_sort


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
