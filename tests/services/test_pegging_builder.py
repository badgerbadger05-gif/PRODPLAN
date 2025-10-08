from __future__ import annotations

from types import SimpleNamespace
from datetime import date
import os
import sys

# Ensure project root on sys.path for "backend" import
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.app.services.pegging_builder import PeggingBuilder  # noqa: E402


def comp(item_id, quantity):
    return SimpleNamespace(item_id=item_id, quantity=quantity)


def test_pegging_builder_simple_one_level():
    """
    Для одного заказа родителя и одной спецификации:
    - формируется одна ссылка PeggingLink на каждого компонента;
    - qty_contribution = order.qty * comp.quantity;
    - need_date = parent_need_date = order.bucket_date (если есть) иначе order.need_date.
    """
    run_id = 123
    order = SimpleNamespace(order_id=1, item_id=10, qty=5.0, bucket_date=date(2025, 1, 10), need_date=date(2025, 1, 11))
    orders = [order]

    default_spec_map = {10: 100}
    comps = [comp(20, 2.0), comp(30, 0.5)]
    def get_components_for_spec(spec_id: int):
        assert spec_id == 100
        return comps

    pb = PeggingBuilder()
    links = pb.build(run_id=run_id, orders=orders, default_spec_map=default_spec_map, get_components_for_spec=get_components_for_spec)

    assert len(links) == 2
    l1 = links[0]
    l2 = links[1]

    # Common fields
    for l in links:
        assert l.run_id == run_id
        assert l.parent_item_id == 10
        assert l.need_date == order.bucket_date
        assert l.parent_need_date == order.bucket_date

    # Quantities
    assert l1.child_item_id in (20, 30)
    assert l2.child_item_id in (20, 30)
    assert l1.child_item_id != l2.child_item_id

    # Contribution math
    expected = {20: 5.0 * 2.0, 30: 5.0 * 0.5}
    assert abs(links[0].qty_contribution - expected[links[0].child_item_id]) < 1e-9
    assert abs(links[1].qty_contribution - expected[links[1].child_item_id]) < 1e-9


def test_pegging_builder_skips_zero_or_missing_spec():
    """
    Если comp.quantity <= 0 — пропускаем.
    Если нет default_spec_map для item — ссылок не формируем.
    """
    run_id = 1
    order_a = SimpleNamespace(order_id=1, item_id=1000, qty=10.0, bucket_date=date(2025, 2, 1), need_date=date(2025, 2, 2))
    order_b = SimpleNamespace(order_id=2, item_id=2000, qty=10.0, bucket_date=date(2025, 2, 1), need_date=date(2025, 2, 2))
    orders = [order_a, order_b]

    # Только для 1000 есть спецификация, но в ней quantity=0 -> пропуск
    default_spec_map = {1000: 500}  # 2000 отсутствует
    def get_components_for_spec(spec_id: int):
        assert spec_id == 500
        return [SimpleNamespace(item_id=777, quantity=0.0)]

    pb = PeggingBuilder()
    links = pb.build(run_id=run_id, orders=orders, default_spec_map=default_spec_map, get_components_for_spec=get_components_for_spec)

    assert links == []