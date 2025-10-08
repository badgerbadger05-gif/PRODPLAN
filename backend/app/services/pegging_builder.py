from __future__ import annotations

from typing import List, Dict, Callable
from ..models import PeggingLink, PlannedOrder, SpecComponent


class PeggingBuilder:
    """
    Build one-level PeggingLink records (child -> parent) for planned production orders.
    Stateless helper; does not touch DB session directly and returns ORM objects to be added by caller.
    """

    def build(
        self,
        run_id: int,
        orders: List[PlannedOrder],
        default_spec_map: Dict[int, int],
        get_components_for_spec: Callable[[int], List[SpecComponent]],
    ) -> List[PeggingLink]:
        links: List[PeggingLink] = []
        if not orders:
            return links

        for order in orders:
            # Parent item (planned order item)
            try:
                parent_item_id = int(getattr(order, "item_id"))
            except Exception:
                continue

            # Resolve specification for parent item (default spec)
            spec_id = default_spec_map.get(int(parent_item_id))
            if not spec_id:
                continue

            # Components of specification (one-level pegging)
            comps = get_components_for_spec(int(spec_id)) or []
            if not comps:
                continue

            # Parent quantity (order qty)
            try:
                qty_parent = float(getattr(order, "qty", 0.0) or 0.0)
            except Exception:
                qty_parent = 0.0
            if qty_parent <= 0.0:
                continue

            for comp in comps:
                # Component item and per-unit quantity
                try:
                    child_id = int(getattr(comp, "item_id"))
                    per_unit = float(getattr(comp, "quantity", 0.0) or 0.0)
                except Exception:
                    continue

                if per_unit <= 0.0:
                    continue

                qty_contrib = float(qty_parent) * float(per_unit)
                if qty_contrib <= 0.0:
                    continue

                # Need date and parent need date follow the order bucket_date (or need_date backup)
                need_dt = getattr(order, "bucket_date", None) or getattr(order, "need_date", None)

                pl = PeggingLink(
                    run_id=int(run_id),
                    child_item_id=int(child_id),
                    parent_item_id=int(parent_item_id),
                    demand_ref=None,
                    qty_contribution=float(qty_contrib),
                    need_date=need_dt,
                    parent_need_date=need_dt,
                )
                links.append(pl)

        return links