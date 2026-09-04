"""Pure global allocation for the canonical assembly readiness gate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any


QTY_QUANTUM = Decimal("0.001")
ROOT_QTY_QUANTUM = Decimal("1")


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


@dataclass(frozen=True)
class ReadinessLine:
    queue_line_id: int
    sort_key: str
    open_qty: Decimal
    component_norms: tuple[tuple[int, Decimal], ...]


@dataclass(frozen=True)
class ComponentBlocker:
    component_item_id: int
    norm_qty_per_root: Decimal
    required_qty: Decimal
    available_qty: Decimal
    shortage_qty: Decimal


@dataclass(frozen=True)
class ReadinessResult:
    queue_line_id: int
    status: str
    open_qty: Decimal
    ready_qty: Decimal
    blockers: tuple[ComponentBlocker, ...]


READINESS_HORIZONS = ("now", "transfer", "kitting", "committed", "launch")


@dataclass(frozen=True)
class FrozenBomEdge:
    bom_key: int
    parent_item_id: int
    component_item_id: int
    norm_qty: Decimal
    root_item_id: int | None = None


@dataclass(frozen=True)
class ReadinessSupply:
    source_key: str
    item_id: int
    qty: Decimal
    layer: str
    warehouse_ref1c: str = ""
    available_date: date | None = None
    confidence: str = "physical"
    bom_key: int | None = None
    queue_line_id: int | None = None


@dataclass(frozen=True)
class ReplenishmentPolicy:
    bom_key: int
    item_id: int
    mode: str
    lead_days: int | None = None
    route_kind: str = ""
    resource_id: int | None = None
    output_warehouse_ref1c: str = ""
    unavailable_reason: str = ""
    root_item_id: int | None = None


@dataclass(frozen=True)
class ReadinessCurveLine:
    queue_line_id: int
    sort_key: str
    bom_key: int
    root_item_id: int
    open_qty: Decimal
    target_warehouse_ref1c: str


@dataclass(frozen=True)
class ReadinessAction:
    action_kind: str
    item_id: int
    qty: Decimal
    available_date: date | None
    confidence: str
    source_key: str = ""
    source_warehouse_ref1c: str = ""
    destination_warehouse_ref1c: str = ""
    resource_id: int | None = None
    path: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReadinessCurvePoint:
    horizon: str
    cumulative_qty: Decimal
    available_date: date | None
    actions: tuple[ReadinessAction, ...]


@dataclass(frozen=True)
class ReadinessBlocker:
    item_id: int
    required_qty: Decimal
    available_qty: Decimal
    shortage_qty: Decimal
    reason: str
    destination_warehouse_ref1c: str = ""
    path: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReadinessCurveResult:
    queue_line_id: int
    open_qty: Decimal
    status: str
    points: tuple[ReadinessCurvePoint, ...]
    unavailable_reasons: tuple[str, ...] = ()
    blockers: tuple[ReadinessBlocker, ...] = ()


_HORIZON_RANK = {name: index for index, name in enumerate(READINESS_HORIZONS)}
_SUPPLY_RANK = {"now": 0, "transfer": 1, "committed": 3}


def _q(value: Decimal) -> Decimal:
    return max(_d(value), Decimal("0")).quantize(QTY_QUANTUM, rounding=ROUND_DOWN)


def _root_q(value: Decimal) -> Decimal:
    """Return an indivisible finished-assembly quantity.

    Component norms and stock may legitimately have thousandth precision, but
    the roots placed on the assembly drum are counted in pieces.  Silently
    truncating a fractional plan would break conservation, so fail closed when
    an upstream queue line itself is not integral.
    """
    quantity = _q(value)
    whole = quantity.quantize(ROOT_QTY_QUANTUM, rounding=ROUND_DOWN)
    if quantity != whole:
        raise ValueError(f"assembly root quantity must be whole, got {quantity}")
    return whole


def _max_date(left: date | None, right: date | None) -> date | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _aggregate_actions(actions: list[ReadinessAction]) -> tuple[ReadinessAction, ...]:
    grouped: dict[tuple[Any, ...], ReadinessAction] = {}
    for row in actions:
        key = (
            row.action_kind,
            row.item_id,
            row.available_date,
            row.confidence,
            row.source_key,
            row.source_warehouse_ref1c,
            row.destination_warehouse_ref1c,
            row.resource_id,
            row.path,
        )
        current = grouped.get(key)
        grouped[key] = row if current is None else replace(current, qty=current.qty + row.qty)
    return tuple(
        sorted(
            grouped.values(),
            key=lambda row: (
                row.available_date or date.min,
                row.action_kind,
                row.item_id,
                row.source_key,
                row.path,
            ),
        )
    )


def allocate_readiness_curves(
    lines: tuple[ReadinessCurveLine, ...],
    edges: tuple[FrozenBomEdge, ...],
    supplies: tuple[ReadinessSupply, ...],
    policies: tuple[ReplenishmentPolicy, ...],
    *,
    as_of: date,
    global_unavailable_reasons: tuple[str, ...] = (),
) -> tuple[ReadinessCurveResult, ...]:
    """Build cumulative readiness scenarios with consume-once allocation.

    Every horizon is an alternative cumulative scenario over the same frozen
    facts.  Inside a scenario sources are shared by all queue rows and consumed
    only after a producible root quantity is proven.  A blocked older row
    therefore cannot hoard stock needed by a younger ready row.
    """
    graph: dict[tuple[int, int, int], list[tuple[int, Decimal]]] = {}
    for edge in edges:
        norm = _d(edge.norm_qty)
        if norm <= 0:
            continue
        graph.setdefault((
            int(edge.bom_key),
            int(edge.root_item_id) if edge.root_item_id is not None else 0,
            int(edge.parent_item_id),
        ), []).append(
            (int(edge.component_item_id), norm)
        )
    for parent_key in graph:
        graph[parent_key].sort(key=lambda row: row[0])
    policy_by_item = {
        (
            int(row.bom_key),
            int(row.root_item_id) if row.root_item_id is not None else 0,
            int(row.item_id),
        ): row
        for row in policies
    }

    def graph_rows(bom_key: int, root_item_id: int, parent_item_id: int):
        return graph.get(
            (int(bom_key), int(root_item_id), int(parent_item_id)),
            graph.get((int(bom_key), 0, int(parent_item_id)), ()),
        )

    def item_policy(bom_key: int, root_item_id: int, item_id: int):
        return policy_by_item.get(
            (int(bom_key), int(root_item_id), int(item_id)),
            policy_by_item.get((int(bom_key), 0, int(item_id))),
        )
    ordered_lines = tuple(sorted(lines, key=lambda row: (str(row.sort_key), int(row.queue_line_id))))
    open_qty_by_line = {
        int(line.queue_line_id): _root_q(line.open_qty)
        for line in ordered_lines
    }
    if global_unavailable_reasons:
        return tuple(
            ReadinessCurveResult(
                queue_line_id=int(line.queue_line_id),
                open_qty=open_qty_by_line[int(line.queue_line_id)],
                status="unavailable",
                points=tuple(
                    ReadinessCurvePoint(horizon, Decimal("0"), None, ())
                    for horizon in READINESS_HORIZONS
                ),
                unavailable_reasons=tuple(sorted(set(global_unavailable_reasons))),
            )
            for line in ordered_lines
        )
    points_by_line: dict[int, list[ReadinessCurvePoint]] = {
        int(row.queue_line_id): [] for row in ordered_lines
    }
    reasons_by_line: dict[int, set[str]] = {
        int(row.queue_line_id): set() for row in ordered_lines
    }
    blockers_by_line: dict[int, tuple[ReadinessBlocker, ...]] = {
        int(row.queue_line_id): () for row in ordered_lines
    }

    for horizon in READINESS_HORIZONS:
        horizon_rank = _HORIZON_RANK[horizon]
        remaining = {row.source_key: _q(row.qty) for row in supplies}
        supply_by_item: dict[int, list[ReadinessSupply]] = {}
        for row in supplies:
            if _SUPPLY_RANK.get(row.layer, 99) <= horizon_rank and _d(row.qty) > 0:
                supply_by_item.setdefault(int(row.item_id), []).append(row)
        for item_rows in supply_by_item.values():
            item_rows.sort(
                key=lambda row: (
                    _SUPPLY_RANK.get(row.layer, 99),
                    row.available_date or date.min,
                    row.warehouse_ref1c,
                    row.source_key,
                )
            )

        for line in ordered_lines:
            line_id = int(line.queue_line_id)
            open_qty = open_qty_by_line[line_id]
            target = str(line.target_warehouse_ref1c or "").strip()
            if not target:
                reasons_by_line[line_id].add("TARGET_WAREHOUSE_MISSING")
                points_by_line[line_id].append(
                    ReadinessCurvePoint(horizon, Decimal("0"), None, ())
                )
                continue
            bom_key = int(line.bom_key)
            root_item_id = int(line.root_item_id)
            if not graph_rows(bom_key, root_item_id, root_item_id):
                reasons_by_line[line_id].add("ROOT_FROZEN_BOM_MISSING")
                points_by_line[line_id].append(
                    ReadinessCurvePoint(horizon, Decimal("0"), None, ())
                )
                continue

            def attempt(root_qty: Decimal, pool: dict[str, Decimal]):
                actions: list[ReadinessAction] = []
                visiting: set[int] = set()

                def blocked(
                    *,
                    item_id: int,
                    required_qty: Decimal,
                    available_qty: Decimal,
                    reason: str,
                    destination: str,
                    path: tuple[int, ...],
                ) -> tuple[bool, None, tuple[ReadinessBlocker, ...]]:
                    required = _q(required_qty)
                    available = _q(available_qty)
                    return False, None, (
                        ReadinessBlocker(
                            item_id=int(item_id),
                            required_qty=required,
                            available_qty=available,
                            shortage_qty=_q(max(required - available, Decimal("0"))),
                            reason=reason,
                            destination_warehouse_ref1c=destination,
                            path=path,
                        ),
                    )

                def fulfill(
                    item_id: int,
                    qty: Decimal,
                    path: tuple[int, ...],
                    destination: str,
                ):
                    requested = _d(qty)
                    needed = requested
                    ready_date: date | None = as_of
                    for source in supply_by_item.get(int(item_id), ()):
                        if needed <= 0:
                            break
                        if source.queue_line_id is not None and int(source.queue_line_id) != line_id:
                            continue
                        if source.bom_key is not None and int(source.bom_key) != bom_key:
                            continue
                        if horizon == "now" and str(source.warehouse_ref1c or "") != destination:
                            continue
                        available = pool.get(source.source_key, Decimal("0"))
                        take = min(needed, available)
                        if take <= 0:
                            continue
                        pool[source.source_key] = available - take
                        needed -= take
                        ready_date = _max_date(ready_date, source.available_date)
                        if source.layer == "committed":
                            actions.append(
                                ReadinessAction(
                                    action_kind="committed_supply",
                                    item_id=int(item_id),
                                    qty=take,
                                    available_date=source.available_date,
                                    confidence=source.confidence,
                                    source_key=source.source_key,
                                    destination_warehouse_ref1c=str(source.warehouse_ref1c or ""),
                                    path=path,
                                )
                            )
                        if str(source.warehouse_ref1c or "") != destination:
                            actions.append(
                                ReadinessAction(
                                    action_kind="transfer",
                                    item_id=int(item_id),
                                    qty=take,
                                    available_date=source.available_date or as_of,
                                    confidence=source.confidence,
                                    source_key=source.source_key,
                                    source_warehouse_ref1c=str(source.warehouse_ref1c or ""),
                                    destination_warehouse_ref1c=destination,
                                    path=path,
                                )
                            )
                    if needed <= Decimal("0.0000001"):
                        return True, ready_date, ()

                    policy = item_policy(bom_key, root_item_id, int(item_id))
                    can_kit = (
                        horizon_rank >= _HORIZON_RANK["kitting"]
                        and policy is not None
                        and policy.route_kind == "kitting"
                    )
                    can_launch = horizon_rank >= _HORIZON_RANK["launch"]
                    if policy is None or (not can_kit and not can_launch):
                        return blocked(
                            item_id=item_id,
                            required_qty=requested,
                            available_qty=requested - needed,
                            reason=(
                                "REPLENISHMENT_POLICY_MISSING"
                                if policy is None
                                else "HORIZON_DOES_NOT_ALLOW_REPLENISHMENT"
                            ),
                            destination=destination,
                            path=path,
                        )
                    if policy.unavailable_reason:
                        return blocked(
                            item_id=item_id,
                            required_qty=requested,
                            available_qty=requested - needed,
                            reason=policy.unavailable_reason,
                            destination=destination,
                            path=path,
                        )
                    if int(item_id) in visiting:
                        return blocked(
                            item_id=item_id,
                            required_qty=requested,
                            available_qty=requested - needed,
                            reason="BOM_CYCLE",
                            destination=destination,
                            path=path,
                        )
                    mode = str(policy.mode or "unavailable")
                    if mode == "buy":
                        if not can_launch or policy.lead_days is None:
                            return blocked(
                                item_id=item_id,
                                required_qty=requested,
                                available_qty=requested - needed,
                                reason="LEAD_TIME_MISSING",
                                destination=destination,
                                path=path,
                            )
                        finish = as_of + timedelta(days=max(int(policy.lead_days), 0))
                        actions.append(
                            ReadinessAction(
                                action_kind="buy",
                                item_id=int(item_id),
                                qty=needed,
                                available_date=finish,
                                confidence="forecast",
                                destination_warehouse_ref1c=destination,
                                path=path,
                            )
                        )
                        return True, _max_date(ready_date, finish), ()
                    if mode not in {"make", "rework"} or not graph_rows(
                        bom_key, root_item_id, int(item_id)
                    ):
                        return blocked(
                            item_id=item_id,
                            required_qty=requested,
                            available_qty=requested - needed,
                            reason=(
                                "FROZEN_BOM_MISSING"
                                if mode in {"make", "rework"}
                                else "REPLENISHMENT_MODE_UNAVAILABLE"
                            ),
                            destination=destination,
                            path=path,
                        )
                    if policy.lead_days is None:
                        return blocked(
                            item_id=item_id,
                            required_qty=requested,
                            available_qty=requested - needed,
                            reason="LEAD_TIME_MISSING",
                            destination=destination,
                            path=path,
                        )
                    visiting.add(int(item_id))
                    child_date: date | None = as_of
                    production_warehouse = str(policy.output_warehouse_ref1c or "").strip()
                    if not production_warehouse:
                        visiting.remove(int(item_id))
                        return blocked(
                            item_id=item_id,
                            required_qty=requested,
                            available_qty=requested - needed,
                            reason="OUTPUT_WAREHOUSE_MISSING",
                            destination=destination,
                            path=path,
                        )
                    for component_id, norm in graph_rows(
                        bom_key, root_item_id, int(item_id)
                    ):
                        ok, component_date, component_blockers = fulfill(
                            component_id,
                            needed * norm,
                            path + (int(item_id),),
                            production_warehouse,
                        )
                        if not ok:
                            visiting.remove(int(item_id))
                            return False, None, component_blockers
                        child_date = _max_date(child_date, component_date)
                    visiting.remove(int(item_id))
                    base = child_date or as_of
                    finish = base + timedelta(days=max(int(policy.lead_days), 0))
                    actions.append(
                        ReadinessAction(
                            action_kind=("kitting" if policy.route_kind == "kitting" else mode),
                            item_id=int(item_id),
                            qty=needed,
                            available_date=finish,
                            confidence="forecast",
                            destination_warehouse_ref1c=production_warehouse,
                            resource_id=policy.resource_id,
                            path=path,
                        )
                    )
                    if production_warehouse != destination:
                        actions.append(
                            ReadinessAction(
                                action_kind="transfer",
                                item_id=int(item_id),
                                qty=needed,
                                available_date=finish,
                                confidence="forecast",
                                source_warehouse_ref1c=production_warehouse,
                                destination_warehouse_ref1c=destination,
                                path=path,
                            )
                        )
                    return True, _max_date(ready_date, finish), ()

                root_date: date | None = as_of
                visiting.add(int(line.root_item_id))
                for component_id, norm in graph_rows(
                    bom_key, root_item_id, root_item_id
                ):
                    ok, component_date, component_blockers = fulfill(
                        component_id,
                        root_qty * norm,
                        (int(line.root_item_id),),
                        target,
                    )
                    if not ok:
                        return False, None, (), component_blockers
                    root_date = _max_date(root_date, component_date)
                return True, root_date, _aggregate_actions(actions), ()

            low = Decimal("0")
            high = open_qty
            best_pool = dict(remaining)
            best_date: date | None = None
            best_actions: tuple[ReadinessAction, ...] = ()
            # Finished assemblies are indivisible.  Component quantities keep
            # their normal precision inside ``attempt``; only the root search
            # advances in whole pieces.
            while high - low >= ROOT_QTY_QUANTUM:
                mid = ((low + high) / 2).quantize(ROOT_QTY_QUANTUM, rounding=ROUND_DOWN)
                if mid <= low:
                    mid = low + ROOT_QTY_QUANTUM
                trial_pool = dict(remaining)
                ok, ready_date, actions, _ = attempt(mid, trial_pool)
                if ok:
                    low = mid
                    best_pool = trial_pool
                    best_date = ready_date
                    best_actions = actions
                else:
                    high = mid - ROOT_QTY_QUANTUM
            if low < open_qty:
                trial_pool = dict(remaining)
                ok, ready_date, actions, full_blockers = attempt(open_qty, trial_pool)
                if ok:
                    low = open_qty
                    best_pool = trial_pool
                    best_date = ready_date
                    best_actions = actions
                elif horizon == "launch":
                    blockers_by_line[line_id] = full_blockers
            remaining = best_pool
            points_by_line[line_id].append(
                ReadinessCurvePoint(horizon, _root_q(low), best_date, best_actions)
            )

    results: list[ReadinessCurveResult] = []
    for line in ordered_lines:
        points = tuple(points_by_line[int(line.queue_line_id)])
        launch_qty = points[-1].cumulative_qty if points else Decimal("0")
        now_qty = points[0].cumulative_qty if points else Decimal("0")
        status = (
            "unavailable" if reasons_by_line[int(line.queue_line_id)]
            else "ready" if now_qty >= open_qty_by_line[int(line.queue_line_id)]
            else "recoverable" if launch_qty >= open_qty_by_line[int(line.queue_line_id)]
            else "partial" if launch_qty > 0
            else "blocked"
        )
        results.append(
            ReadinessCurveResult(
                queue_line_id=int(line.queue_line_id),
                open_qty=open_qty_by_line[int(line.queue_line_id)],
                status=status,
                points=points,
                unavailable_reasons=tuple(sorted(reasons_by_line[int(line.queue_line_id)])),
                blockers=blockers_by_line[int(line.queue_line_id)],
            )
        )
    return tuple(results)


def allocate_assembly_readiness(
    lines: tuple[ReadinessLine, ...],
    free_stock_by_item: dict[int, Decimal] | None,
) -> tuple[ReadinessResult, ...]:
    """Allocate shared stock once, oldest-first, without a second BOM explosion."""
    ordered_lines = tuple(
        sorted(lines, key=lambda row: (str(row.sort_key), int(row.queue_line_id)))
    )
    open_qty_by_line = {
        int(line.queue_line_id): _root_q(line.open_qty)
        for line in ordered_lines
    }
    if free_stock_by_item is None:
        return tuple(
            ReadinessResult(
                queue_line_id=int(line.queue_line_id),
                status="unavailable",
                open_qty=open_qty_by_line[int(line.queue_line_id)],
                ready_qty=Decimal("0"),
                blockers=(),
            )
            for line in ordered_lines
        )
    available = {
        int(item_id): max(_d(qty), Decimal("0"))
        for item_id, qty in free_stock_by_item.items()
    }
    results: list[ReadinessResult] = []
    for line in ordered_lines:
        open_qty = open_qty_by_line[int(line.queue_line_id)]
        norms: dict[int, Decimal] = {}
        for component_item_id, raw_norm in line.component_norms:
            norm = _d(raw_norm)
            if norm <= 0:
                continue
            iid = int(component_item_id)
            norms[iid] = norms.get(iid, Decimal("0")) + norm

        if open_qty <= 0:
            results.append(ReadinessResult(line.queue_line_id, "ready", open_qty, open_qty, ()))
            continue
        if not norms:
            results.append(ReadinessResult(line.queue_line_id, "unavailable", open_qty, Decimal("0"), ()))
            continue

        ready_qty = open_qty
        for component_item_id, norm in norms.items():
            possible = (available.get(component_item_id, Decimal("0")) / norm).quantize(
                ROOT_QTY_QUANTUM,
                rounding=ROUND_DOWN,
            )
            ready_qty = min(ready_qty, max(possible, Decimal("0")))
        ready_qty = ready_qty.quantize(ROOT_QTY_QUANTUM, rounding=ROUND_DOWN)

        for component_item_id, norm in norms.items():
            available[component_item_id] = max(
                available.get(component_item_id, Decimal("0")) - ready_qty * norm,
                Decimal("0"),
            )

        blocked_root_qty = max(open_qty - ready_qty, Decimal("0"))
        blockers: list[ComponentBlocker] = []
        if blocked_root_qty > 0:
            for component_item_id, norm in sorted(norms.items()):
                required = blocked_root_qty * norm
                component_available = available.get(component_item_id, Decimal("0"))
                shortage = max(required - component_available, Decimal("0"))
                if shortage <= 0:
                    continue
                blockers.append(
                    ComponentBlocker(
                        component_item_id=component_item_id,
                        norm_qty_per_root=norm,
                        required_qty=required,
                        available_qty=component_available,
                        shortage_qty=shortage,
                    )
                )

        status = "ready" if ready_qty >= open_qty else "partial" if ready_qty > 0 else "blocked"
        results.append(
            ReadinessResult(
                queue_line_id=int(line.queue_line_id),
                status=status,
                open_qty=open_qty,
                ready_qty=ready_qty,
                blockers=tuple(blockers),
            )
        )
    return tuple(results)
