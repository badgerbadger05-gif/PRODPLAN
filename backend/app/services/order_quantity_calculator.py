from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple, Callable, Set
from .warnings import make_warning


class OrderQuantityCalculator:
    """
    Calculate production order quantities with:
    - buffer days by area (resource.buffer_days) using average daily demand
    - optimal batch priority over buffer
    - component availability limit (by current stock + WIP)
    - horizon demand limit (sum net demand within horizon)
    - lot sizing normalization (min_batch/multiple/rounding)

    This class is stateless regarding DB; it operates on pre-fetched dictionaries/caches.
    """

    def __init__(
        self,
        snapshot: Dict[str, Any],
        default_spec_map: Dict[int, int],
        spec_by_id: Dict[int, Any],
        components_loader: Callable[[int], List[Any]],
        item_by_id: Dict[int, Any],
        units_by_ref: Optional[Dict[str, Any]] = None,
        res_by_id: Dict[int, Any] = None,
        production_kinds_by_resource: Dict[int, Set[int]] = None,
        stock_by_item: Dict[int, float] = None,
        wip_by_item: Dict[int, float] = None,
        horizon_days: int = 0,
        total_demand_by_item: Dict[int, float] = None,
    ) -> None:
        self.snapshot = snapshot or {}
        self.default_spec_map = default_spec_map or {}
        self.spec_by_id = spec_by_id or {}
        self.components_loader = components_loader
        self.item_by_id = item_by_id or {}
        self.units_by_ref = units_by_ref or {}
        self.res_by_id = res_by_id or {}
        self.production_kinds_by_resource = production_kinds_by_resource or {}
        self.stock_by_item = stock_by_item or {}
        self.wip_by_item = wip_by_item or {}
        self.horizon_days = int(horizon_days or 0)
        self.total_demand_by_item = total_demand_by_item or {}

    # --- Public API ---

    def compute(self, item_id: int, requested_qty: float) -> Tuple[float, float, Dict[str, Any], List[Dict[str, Any]]]:
        """
        Returns:
          (final_qty_before_lotsizing, normalized_qty, computation_details, warnings)

        where:
          - final_qty_before_lotsizing = min(requested_qty, horizon_total_demand) [capped to integer for discrete units]
          - normalized_qty = lot-sized qty considering optimal_batch and buffer
          - computation_details =
              {
                'requested_qty': float,
                'buffer_qty': float,
                'horizon_limit': float,
                'component_limit': float,  # integer for discrete units
                'final_qty_before_capacity': float,
                'normalized_qty': float
              }
        """
        warnings: List[Dict[str, Any]] = []
        item = self.item_by_id.get(int(item_id))
        is_discrete = self._is_discrete_unit_by_item(int(item_id))

        # 1) Buffer qty by area buffer_days and average daily demand
        buffer_qty = self._calculate_buffer_qty(item_id)

        # 2) Horizon demand limit (integer for discrete units)
        total_horizon_demand = float(self.total_demand_by_item.get(int(item_id), 0.0) or 0.0)
        if is_discrete:
            try:
                total_horizon_demand = math.floor(total_horizon_demand + 1e-9)
            except Exception:
                total_horizon_demand = float(int(total_horizon_demand))

        # 3) Components availability limit (based on default spec if any)
        #    We compute it for diagnostics, rounding down to whole units for discrete parents.
        component_limit = None
        spec_id = self.default_spec_map.get(int(item_id))
        if spec_id:
            comp_limit, comp_warnings = self._limit_by_components(int(spec_id), float(requested_qty or 0.0), int(item_id))
            component_limit = float(comp_limit)
            warnings.extend(comp_warnings)

        # 4) Compose final quantity before lot sizing:
        final_qty = min(float(requested_qty or 0.0), float(total_horizon_demand or 0.0))
        if final_qty < 0.0:
            final_qty = 0.0
        if is_discrete:
            final_qty = float(math.floor(final_qty + 1e-9))

        # 5) Lot sizing for production with optimal_batch priority over buffer
        # Important: normalized_qty may exceed requested "final_qty" due to buffer/optimal batch preferences.
        # Hard caps (horizon/components) are applied later at planning phase (build_planned_orders_and_purchases).
        normalized_qty = self._normalize_qty_for_production(final_qty, item, buffer_qty)

        computation_details: Dict[str, Any] = {
            "requested_qty": float(requested_qty or 0.0),
            "buffer_qty": float(buffer_qty),
            "horizon_limit": float(total_horizon_demand),
            "component_limit": float(component_limit if component_limit is not None else (requested_qty or 0.0)),
            "final_qty_before_capacity": float(final_qty),
            "normalized_qty": float(normalized_qty),
        }

        return float(final_qty), float(normalized_qty), computation_details, warnings

    # --- Internals ---

    def _normalize_lot_qty(self, qty: float, min_qty: Optional[float], multiple: Optional[float], rounding: str) -> float:
        try:
            q = float(qty or 0.0)
        except Exception:
            q = 0.0
        if q <= 0.0:
            return 0.0

        mn = None
        try:
            mn = float(min_qty) if (min_qty is not None) else None
        except Exception:
            mn = None
        if mn is not None and mn > 0.0 and q < mn:
            q = mn

        m = None
        try:
            m = float(multiple) if (multiple is not None) else None
        except Exception:
            m = None
        mode = (rounding or "ceil").strip().lower()

        if m is not None and m > 0.0:
            ratio = q / m
            if mode == "floor":
                q = math.floor(ratio) * m
            elif mode == "round":
                q = round(ratio) * m
            else:
                q = math.ceil(ratio) * m

            if mn is not None and mn > 0.0 and q < mn:
                q = math.ceil(mn / m) * m

        if not (q > 0.0):
            return 0.0
        return float(q)

    def _normalize_qty_for_production(self, qty: float, item: Optional[Any], buffer_qty: Optional[float]) -> float:
        """
        Priority:
        1) item.optimal_batch if present (wins over buffer)
        2) buffer quantity (if provided)
        3) min_batch/multiple/rounding from snapshot.production.lot_sizing
        """
        lot_cfg: Dict[str, Any] = {}
        if isinstance(self.snapshot, dict):
            lot_cfg = (self.snapshot.get("production") or {}).get("lot_sizing") or {}

        try:
            min_batch = float(lot_cfg.get("min_batch", 1) or 1)
        except Exception:
            min_batch = 1.0
        try:
            multiple = float(lot_cfg.get("multiple", 1) or 1)
        except Exception:
            multiple = 1.0
        rounding = str(lot_cfg.get("rounding", "ceil") or "ceil").strip().lower()

        # Optimal batch from item card
        optimal_batch = None
        if item is not None:
            try:
                ob = getattr(item, "optimal_batch", None)
                if ob is not None:
                    ob = float(ob)
                    if ob > 0.0:
                        optimal_batch = ob
            except Exception:
                optimal_batch = None

        base_qty = float(qty or 0.0)
        if buffer_qty is not None and buffer_qty > 0.0:
            base_qty = max(base_qty, float(buffer_qty))

        if optimal_batch is not None and optimal_batch > 0.0:
            if optimal_batch >= base_qty:
                base_qty = optimal_batch
            else:
                ratio = base_qty / optimal_batch
                if rounding == "floor":
                    base_qty = math.floor(ratio) * optimal_batch
                elif rounding == "round":
                    base_qty = round(ratio) * optimal_batch
                else:
                    base_qty = math.ceil(ratio) * optimal_batch

        return self._normalize_lot_qty(base_qty, min_batch, multiple, rounding)

    def _calculate_buffer_qty(self, item_id: int) -> float:
        """
        Buffer qty = avg_daily_demand * buffer_days(area)
        - buffer_days is taken from the first mapped resource for the item's production kind
        - avg_daily_demand computed as total demand in horizon divided by horizon_days (not count of buckets)
        """
        buffer_days = 0
        # 1) get production_kind from default spec
        spec_id = self.default_spec_map.get(int(item_id))
        production_kind_id = None
        if spec_id:
            spec = self.spec_by_id.get(int(spec_id))
            if spec:
                production_kind_id = getattr(spec, "production_kind_id", None)

        # 2) find first resource candidate for this production kind to read buffer_days
        if production_kind_id is not None:
            candidates = [rid for rid, pkset in self.production_kinds_by_resource.items() if production_kind_id in pkset]
            if candidates:
                res = self.res_by_id.get(int(candidates[0]))
                if res is not None:
                    try:
                        buffer_days = int(getattr(res, "buffer_days", 0) or 0)
                    except Exception:
                        buffer_days = 0

        if buffer_days <= 0 or self.horizon_days <= 0:
            return 0.0

        total_demand = float(self.total_demand_by_item.get(int(item_id), 0.0) or 0.0)
        if total_demand <= 0.0:
            return 0.0

        avg_daily_demand = total_demand / float(max(1, self.horizon_days))
        return float(avg_daily_demand * buffer_days)

    def _is_discrete_unit_by_item(self, item_id: int) -> bool:
        """
        Heuristic to determine if an item must be planned in whole units (шт).
        Priority:
          - units.precision == 0 -> discrete
          - units.short_name in {'шт','pcs','pc'} -> discrete
          - units.short_name in {'кг','kg','м','m','мм','cm','л'} -> metric (not discrete)
        Fallback: treat as discrete.
        """
        try:
            item = self.item_by_id.get(int(item_id))
            ref = getattr(item, "unit", None) if item is not None else None
            if ref and self.units_by_ref:
                u = self.units_by_ref.get(ref)
                if u is not None:
                    try:
                        prec = getattr(u, "precision", None)
                        if prec is not None and int(prec) == 0:
                            return True
                    except Exception:
                        pass
                    short = str(getattr(u, "short_name", None) or "").strip().lower()
                    if short in {"шт", "pcs", "pc"}:
                        return True
                    if short in {"кг", "kg", "м", "m", "мм", "cm", "л", "l"}:
                        return False
            return True
        except Exception:
            return True

    def _limit_by_components(self, spec_id: int, requested_qty: float, parent_item_id: int) -> Tuple[float, List[Dict[str, Any]]]:
        """
        Limit possible production by availability of components:
        possible_from_child = (stock + wip) / qty_per_unit
        Note: returns RAW possible qty (may be fractional). Discrete rounding is handled later.
        Returns (max_producible_qty, warnings_list)
        """
        warnings: List[Dict[str, Any]] = []
 
        max_producible = float(requested_qty or 0.0)
        if max_producible <= 0.0:
            return 0.0, warnings
 
        comps = self.components_loader(int(spec_id)) or []
        for comp in comps:
            try:
                child_id = int(getattr(comp, "item_id"))
                per_unit = float(getattr(comp, "quantity", 0.0) or 0.0)
                if per_unit <= 0.0:
                    continue
            except Exception:
                continue
 
        child_stock = None
        for comp in comps:
            try:
                child_id = int(getattr(comp, "item_id"))
                per_unit = float(getattr(comp, "quantity", 0.0) or 0.0)
                if per_unit <= 0.0:
                    continue
                child_stock = float(self.stock_by_item.get(child_id, 0.0) or 0.0)
                child_wip = float(self.wip_by_item.get(child_id, 0.0) or 0.0)
                available_child = child_stock + child_wip
                possible = available_child / per_unit if per_unit > 0.0 else 0.0
                if possible < max_producible:
                    if possible < float(requested_qty or 0.0) - 1e-9:
                        warnings.append(
                            make_warning(
                                "COMPONENT_SHORTAGE",
                                f"Component shortage limits production: component_id={child_id}",
                                component_id=int(child_id),
                                requested_qty=float(requested_qty),
                                max_producible_from_component=float(possible),
                            )
                        )
                    max_producible = possible
            except Exception:
                continue
 
        if max_producible < 0.0:
            max_producible = 0.0
 
        return float(max_producible), warnings