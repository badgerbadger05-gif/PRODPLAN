from backend.app.routers.purchase_control import _canonical_purchase_sort


def test_purchase_sort_keeps_nulls_last_and_ties_ascending():
    rows = [
        {"row_key": "b", "delivery_date": None, "order_number": "2"},
        {"row_key": "z", "delivery_date": "2026-09-10", "order_number": "10"},
        {"row_key": "a", "delivery_date": "2026-09-10", "order_number": "2"},
    ]

    _canonical_purchase_sort(rows, field="delivery_date", descending=True)

    assert [row["row_key"] for row in rows] == ["a", "z", "b"]
