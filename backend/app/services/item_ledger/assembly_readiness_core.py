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
    parent_spec_ref: str = ""
    child_spec_ref: str = ""


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
    transfer_destination_warehouse_ref1c: str = ""
    root_item_ids: tuple[int, ...] = ()
    custody_owner_item_id: int | None = None
    source_kind: str = ""
    source_ref: str = ""
    routable_destination_warehouse_refs: tuple[str, ...] = ()


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
    spec_ref: str = ""
    material_warehouse_ref1c: str = ""


@dataclass(frozen=True)
class ReadinessCurveLine:
    queue_line_id: int
    sort_key: str
    bom_key: int
    root_item_id: int
    open_qty: Decimal
    target_warehouse_ref1c: str
    root_spec_ref: str = ""
    unavailable_reasons: tuple[str, ...] = ()


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
    # A point is a saved answer for this exact horizon, including why the
    # still-open residue cannot advance and which successful prerequisite
    # actions were discovered while evaluating that residue.  Keeping these
    # beside the point prevents the UI from pretending the launch blocker also
    # explains the earlier transfer/kitting horizons.
    blockers: tuple[ReadinessBlocker, ...] = ()
    required_actions: tuple[ReadinessAction, ...] = ()


@dataclass(frozen=True)
class ReadinessCoverageSource:
    coverage_kind: str
    qty: Decimal
    source_key: str
    warehouse_ref1c: str = ""
    destination_warehouse_ref1c: str = ""
    available_date: date | None = None
    confidence: str = "physical"
    source_kind: str = ""
    source_ref: str = ""


@dataclass(frozen=True)
class ReadinessBlocker:
    item_id: int
    required_qty: Decimal
    available_qty: Decimal
    shortage_qty: Decimal
    reason: str
    destination_warehouse_ref1c: str = ""
    path: tuple[int, ...] = ()
    point_of_use_qty: Decimal = Decimal("0")
    custody_qty: Decimal = Decimal("0")
    transit_qty: Decimal = Decimal("0")
    wip_qty: Decimal = Decimal("0")
    supplier_qty: Decimal = Decimal("0")
    other_stock_qty: Decimal = Decimal("0")
    coverage_sources: tuple[ReadinessCoverageSource, ...] = ()


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


def _aggregate_coverage_sources(
    sources: list[ReadinessCoverageSource],
) -> tuple[ReadinessCoverageSource, ...]:
    grouped: dict[tuple[Any, ...], ReadinessCoverageSource] = {}
    for row in sources:
        key = (
            row.coverage_kind,
            row.source_key,
            row.warehouse_ref1c,
            row.destination_warehouse_ref1c,
            row.available_date,
            row.confidence,
            row.source_kind,
            row.source_ref,
        )
        current = grouped.get(key)
        grouped[key] = (
            row
            if current is None
            else replace(current, qty=_q(current.qty + row.qty))
        )
    return tuple(
        sorted(
            grouped.values(),
            key=lambda row: (
                row.coverage_kind,
                row.available_date or date.min,
                row.warehouse_ref1c,
                row.source_ref,
                row.source_key,
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
    allocation_deadline_by_line: dict[int, date] | None = None,
    physical_material_gate: bool = False,
) -> tuple[ReadinessCurveResult, ...]:
    """Build one cumulative consume-once readiness allocation.

    Supply layers open successively across the five horizons, but a physical or
    committed unit is consumed at most once across the entire curve.  A blocked
    older row does not hoard a component: a younger row that can really assemble
    may consume it, and the older row cannot claim the same unit again at a later
    horizon.
    """
    graph: dict[tuple[int, int, int, str], list[tuple[int, str, Decimal]]] = {}
    for edge in edges:
        norm = _d(edge.norm_qty)
        if norm <= 0:
            continue
        graph.setdefault((
            int(edge.bom_key),
            int(edge.root_item_id) if edge.root_item_id is not None else 0,
            int(edge.parent_item_id),
            str(edge.parent_spec_ref or ""),
        ), []).append(
            (int(edge.component_item_id), str(edge.child_spec_ref or ""), norm)
        )
    for parent_key in graph:
        graph[parent_key].sort(key=lambda row: row[0])
    policy_by_item = {
        (
            int(row.bom_key),
            int(row.root_item_id) if row.root_item_id is not None else 0,
            int(row.item_id),
            str(row.spec_ref or ""),
        ): row
        for row in policies
    }

    def graph_rows(
        bom_key: int,
        root_item_id: int,
        parent_item_id: int,
        parent_spec_ref: str,
    ):
        return graph.get(
            (
                int(bom_key),
                int(root_item_id),
                int(parent_item_id),
                str(parent_spec_ref or ""),
            ),
            graph.get(
                (
                    int(bom_key),
                    0,
                    int(parent_item_id),
                    str(parent_spec_ref or ""),
                ),
                (),
            ),
        )

    def item_policy(
        bom_key: int,
        root_item_id: int,
        item_id: int,
        spec_ref: str,
    ):
        return policy_by_item.get(
            (
                int(bom_key),
                int(root_item_id),
                int(item_id),
                str(spec_ref or ""),
            ),
            policy_by_item.get(
                (int(bom_key), 0, int(item_id), str(spec_ref or ""))
            ),
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
    remaining = {row.source_key: _q(row.qty) for row in supplies}
    secured_by_line = {
        int(row.queue_line_id): Decimal("0") for row in ordered_lines
    }
    ready_date_by_line: dict[int, date | None] = {
        int(row.queue_line_id): None for row in ordered_lines
    }
    actions_by_line: dict[int, list[ReadinessAction]] = {
        int(row.queue_line_id): [] for row in ordered_lines
    }

    allocation_passes = (
        ((horizon, (line,)) for line in ordered_lines for horizon in READINESS_HORIZONS)
        if physical_material_gate
        else ((horizon, ordered_lines) for horizon in READINESS_HORIZONS)
    )
    for horizon, horizon_lines in allocation_passes:
        horizon_rank = _HORIZON_RANK[horizon]
        supply_by_item: dict[int, list[ReadinessSupply]] = {}
        for row in supplies:
            if physical_material_gate and row.layer == "committed":
                continue
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

        for line in horizon_lines:
            line_id = int(line.queue_line_id)
            open_qty = open_qty_by_line[line_id]
            secured_qty = secured_by_line[line_id]
            residual_qty = _root_q(max(open_qty - secured_qty, Decimal("0")))
            if line.unavailable_reasons:
                reasons_by_line[line_id].update(line.unavailable_reasons)
                points_by_line[line_id].append(
                    ReadinessCurvePoint(horizon, Decimal("0"), None, ())
                )
                continue
            target = str(line.target_warehouse_ref1c or "").strip()
            if not target:
                reasons_by_line[line_id].add("NO_WAREHOUSE_BINDING")
                points_by_line[line_id].append(
                    ReadinessCurvePoint(horizon, Decimal("0"), None, ())
                )
                continue
            bom_key = int(line.bom_key)
            root_item_id = int(line.root_item_id)
            if not graph_rows(
                bom_key,
                root_item_id,
                root_item_id,
                line.root_spec_ref,
            ):
                reasons_by_line[line_id].add("ROOT_FROZEN_BOM_MISSING")
                points_by_line[line_id].append(
                    ReadinessCurvePoint(horizon, Decimal("0"), None, ())
                )
                continue

            if residual_qty <= 0:
                points_by_line[line_id].append(
                    ReadinessCurvePoint(
                        horizon,
                        secured_qty,
                        ready_date_by_line[line_id],
                        _aggregate_actions(actions_by_line[line_id]),
                    )
                )
                continue

            def attempt(root_qty: Decimal, pool: dict[str, Decimal]):
                actions: list[ReadinessAction] = []
                visiting: set[tuple[int, str]] = set()

                def source_matches(
                    source: ReadinessSupply,
                    path: tuple[int, ...],
                ) -> bool:
                    if (
                        source.queue_line_id is not None
                        and int(source.queue_line_id) != line_id
                    ):
                        return False
                    if (
                        source.bom_key is not None
                        and int(source.bom_key) != bom_key
                    ):
                        return False
                    if (
                        source.root_item_ids
                        and int(root_item_id) not in source.root_item_ids
                    ):
                        return False
                    if source.custody_owner_item_id is not None and (
                        not path
                        or int(path[-1]) != int(source.custody_owner_item_id)
                    ):
                        return False
                    return True

                def coverage_kind(
                    source: ReadinessSupply,
                    *,
                    source_warehouse: str,
                    destination: str,
                ) -> str:
                    if source.layer == "committed":
                        return (
                            "supplier_order"
                            if source.source_kind == "supplier_order"
                            else "wip_order"
                        )
                    if source.confidence == "custody":
                        return "transit" if source.layer == "transfer" else "custody"
                    if source_warehouse != destination:
                        return "transit"
                    return "point_of_use"

                def blocked(
                    *,
                    item_id: int,
                    required_qty: Decimal,
                    available_qty: Decimal,
                    reason: str,
                    destination: str,
                    path: tuple[int, ...],
                    coverage_sources: list[ReadinessCoverageSource],
                    other_stock_sources: list[ReadinessCoverageSource],
                ) -> tuple[bool, None, tuple[ReadinessBlocker, ...]]:
                    required = _q(required_qty)
                    available = _q(available_qty)
                    coverage = _aggregate_coverage_sources(
                        [*coverage_sources, *other_stock_sources]
                    )

                    def coverage_qty(kind: str) -> Decimal:
                        return _q(
                            sum(
                                (row.qty for row in coverage if row.coverage_kind == kind),
                                Decimal("0"),
                            )
                        )

                    return False, None, (
                        ReadinessBlocker(
                            item_id=int(item_id),
                            required_qty=required,
                            available_qty=available,
                            shortage_qty=_q(max(required - available, Decimal("0"))),
                            reason=reason,
                            destination_warehouse_ref1c=destination,
                            path=path,
                            point_of_use_qty=coverage_qty("point_of_use"),
                            custody_qty=coverage_qty("custody"),
                            transit_qty=coverage_qty("transit"),
                            wip_qty=coverage_qty("wip_order"),
                            supplier_qty=coverage_qty("supplier_order"),
                            other_stock_qty=coverage_qty("other_stock"),
                            coverage_sources=coverage,
                        ),
                    )

                def fulfill(
                    item_id: int,
                    spec_ref: str,
                    qty: Decimal,
                    path: tuple[int, ...],
                    destination: str,
                ):
                    requested = _d(qty)
                    needed = requested
                    ready_date: date | None = as_of
                    coverage_taken: list[ReadinessCoverageSource] = []
                    for source in supply_by_item.get(int(item_id), ()):
                        if needed <= 0:
                            break
                        if not source_matches(source, path):
                            continue
                        source_warehouse = str(source.warehouse_ref1c or "")
                        if source_warehouse != destination:
                            if horizon == "now":
                                continue
                            if (
                                str(
                                    source.transfer_destination_warehouse_ref1c
                                    or ""
                                )
                                != destination
                                and destination not in source.routable_destination_warehouse_refs
                            ):
                                continue
                        available = pool.get(source.source_key, Decimal("0"))
                        take = min(needed, available)
                        if take <= 0:
                            continue
                        pool[source.source_key] = available - take
                        needed -= take
                        ready_date = _max_date(ready_date, source.available_date)
                        coverage_taken.append(
                            ReadinessCoverageSource(
                                coverage_kind=coverage_kind(
                                    source,
                                    source_warehouse=source_warehouse,
                                    destination=destination,
                                ),
                                qty=_q(take),
                                source_key=source.source_key,
                                warehouse_ref1c=source_warehouse,
                                destination_warehouse_ref1c=destination,
                                available_date=source.available_date,
                                confidence=source.confidence,
                                source_kind=source.source_kind,
                                source_ref=source.source_ref,
                            )
                        )
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
                        if source_warehouse != destination:
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

                    def other_stock_sources() -> list[ReadinessCoverageSource]:
                        rows: list[ReadinessCoverageSource] = []
                        for source in supply_by_item.get(int(item_id), ()):
                            if (
                                source.layer != "now"
                                or source.confidence != "physical"
                                or not source_matches(source, path)
                            ):
                                continue
                            source_warehouse = str(source.warehouse_ref1c or "")
                            if not source_warehouse or source_warehouse == destination:
                                continue
                            available = _q(
                                pool.get(source.source_key, Decimal("0"))
                            )
                            if available <= 0:
                                continue
                            rows.append(
                                ReadinessCoverageSource(
                                    coverage_kind="other_stock",
                                    qty=available,
                                    source_key=source.source_key,
                                    warehouse_ref1c=source_warehouse,
                                    destination_warehouse_ref1c=destination,
                                    confidence=source.confidence,
                                    source_kind=source.source_kind,
                                    source_ref=source.source_ref,
                                )
                            )
                        return rows

                    def fail(reason: str):
                        return blocked(
                            item_id=item_id,
                            required_qty=requested,
                            available_qty=requested - needed,
                            reason=reason,
                            destination=destination,
                            path=path,
                            coverage_sources=coverage_taken,
                            other_stock_sources=other_stock_sources(),
                        )

                    if needed <= Decimal("0.0000001"):
                        return True, ready_date, ()

                    policy = item_policy(
                        bom_key,
                        root_item_id,
                        int(item_id),
                        spec_ref,
                    )
                    can_kit = (
                        horizon_rank >= _HORIZON_RANK["kitting"]
                        and policy is not None
                        and policy.route_kind == "kitting"
                    )
                    can_launch = horizon_rank >= _HORIZON_RANK["launch"]
                    if policy is None or (not can_kit and not can_launch):
                        return fail(
                            (
                                "REPLENISHMENT_POLICY_MISSING"
                                if policy is None
                                else "HORIZON_DOES_NOT_ALLOW_REPLENISHMENT"
                            )
                        )
                    if policy.unavailable_reason:
                        return fail(policy.unavailable_reason)
                    visit_scope = (int(item_id), str(spec_ref or ""))
                    if visit_scope in visiting:
                        return fail("BOM_CYCLE")
                    mode = str(policy.mode or "unavailable")
                    if mode == "buy":
                        if physical_material_gate:
                            return fail("PURCHASED_COMPONENT_SHORTAGE")
                        if not can_launch or policy.lead_days is None:
                            return fail("LEAD_TIME_MISSING")
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
                        bom_key,
                        root_item_id,
                        int(item_id),
                        spec_ref,
                    ):
                        return fail(
                            (
                                "FROZEN_BOM_MISSING"
                                if mode in {"make", "rework"}
                                else "REPLENISHMENT_MODE_UNAVAILABLE"
                            )
                        )
                    if policy.lead_days is None and not physical_material_gate:
                        return fail("LEAD_TIME_MISSING")
                    visiting.add(visit_scope)
                    child_date: date | None = as_of
                    nested_blockers: list[ReadinessBlocker] = []
                    material_warehouse = str(
                        policy.material_warehouse_ref1c
                        or policy.output_warehouse_ref1c
                        or ""
                    ).strip()
                    output_warehouse = str(
                        policy.output_warehouse_ref1c or ""
                    ).strip()
                    if not material_warehouse or not output_warehouse:
                        visiting.remove(visit_scope)
                        return fail("NO_WAREHOUSE_BINDING")
                    for component_id, child_spec_ref, norm in graph_rows(
                        bom_key,
                        root_item_id,
                        int(item_id),
                        spec_ref,
                    ):
                        ok, component_date, component_blockers = fulfill(
                            component_id,
                            child_spec_ref,
                            needed * norm,
                            path + (int(item_id),),
                            material_warehouse,
                        )
                        if not ok:
                            nested_blockers.extend(component_blockers)
                            continue
                        child_date = _max_date(child_date, component_date)
                    visiting.remove(visit_scope)
                    if nested_blockers:
                        return False, None, tuple(nested_blockers)
                    base = child_date or as_of
                    # Material feasibility is independent of production orders
                    # and default lead-time estimates. The actions describe
                    # work still required, never physical output already made.
                    finish = as_of if physical_material_gate else base + timedelta(days=max(int(policy.lead_days), 0))
                    actions.append(
                        ReadinessAction(
                            action_kind=("kitting" if policy.route_kind == "kitting" else mode),
                            item_id=int(item_id),
                            qty=needed,
                            available_date=finish,
                            confidence="required" if physical_material_gate else "forecast",
                            destination_warehouse_ref1c=output_warehouse,
                            resource_id=policy.resource_id,
                            path=path,
                        )
                    )
                    if output_warehouse != destination:
                        actions.append(
                            ReadinessAction(
                                action_kind="transfer",
                                item_id=int(item_id),
                                qty=needed,
                                available_date=finish,
                                confidence="required" if physical_material_gate else "forecast",
                                source_warehouse_ref1c=output_warehouse,
                                destination_warehouse_ref1c=destination,
                                path=path,
                            )
                        )
                    return True, _max_date(ready_date, finish), ()

                root_date: date | None = as_of
                root_blockers: list[ReadinessBlocker] = []
                visiting.add((int(line.root_item_id), str(line.root_spec_ref or "")))
                for component_id, child_spec_ref, norm in graph_rows(
                    bom_key,
                    root_item_id,
                    root_item_id,
                    line.root_spec_ref,
                ):
                    ok, component_date, component_blockers = fulfill(
                        component_id,
                        child_spec_ref,
                        root_qty * norm,
                        (int(line.root_item_id),),
                        target,
                    )
                    if not ok:
                        root_blockers.extend(component_blockers)
                        continue
                    root_date = _max_date(root_date, component_date)
                if root_blockers:
                    return (
                        False,
                        None,
                        _aggregate_actions(actions),
                        tuple(root_blockers),
                    )
                deadline = (allocation_deadline_by_line or {}).get(line_id)
                if deadline is not None and root_date is not None and root_date > deadline:
                    # A forecast outside this resource's calendar must not
                    # reserve today's shared stock ahead of schedulable work.
                    # Keep its actions/ETA as an explanation, but roll back
                    # the trial pool just like any other infeasible batch.
                    return False, root_date, _aggregate_actions(actions), (
                        ReadinessBlocker(
                            item_id=root_item_id,
                            required_qty=root_qty,
                            available_qty=Decimal("0"),
                            shortage_qty=root_qty,
                            reason="OUTSIDE_DRUM_HORIZON",
                            destination_warehouse_ref1c=target,
                        ),
                    )
                return True, root_date, _aggregate_actions(actions), ()

            low = Decimal("0")
            high = residual_qty
            best_pool = dict(remaining)
            best_date: date | None = None
            best_actions: tuple[ReadinessAction, ...] = ()
            horizon_blockers: tuple[ReadinessBlocker, ...] = ()
            horizon_required_actions: tuple[ReadinessAction, ...] = ()
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
            if low < residual_qty:
                # Explain only the residue left after the largest feasible
                # integer batch.  This makes blockers and required actions
                # additive to the cumulative quantity already promised at the
                # current horizon.
                outstanding = _root_q(residual_qty - low)
                trial_pool = dict(best_pool)
                ok, ready_date, actions, full_blockers = attempt(
                    outstanding, trial_pool
                )
                if ok:
                    low = residual_qty
                    best_pool = trial_pool
                    best_date = _max_date(best_date, ready_date)
                    best_actions = _aggregate_actions(
                        [*best_actions, *actions]
                    )
                else:
                    horizon_blockers = full_blockers
                    horizon_required_actions = actions
                    if horizon == "launch":
                        blockers_by_line[line_id] = full_blockers
            remaining = best_pool
            if low > 0:
                secured_by_line[line_id] = _root_q(secured_qty + low)
                ready_date_by_line[line_id] = _max_date(
                    ready_date_by_line[line_id], best_date
                )
                actions_by_line[line_id].extend(best_actions)
            points_by_line[line_id].append(
                ReadinessCurvePoint(
                    horizon,
                    secured_by_line[line_id],
                    ready_date_by_line[line_id],
                    _aggregate_actions(actions_by_line[line_id]),
                    horizon_blockers,
                    horizon_required_actions,
                )
            )

    results: list[ReadinessCurveResult] = []
    for line in ordered_lines:
        points = tuple(points_by_line[int(line.queue_line_id)])
        launch_qty = points[-1].cumulative_qty if points else Decimal("0")
        now_qty = points[0].cumulative_qty if points else Decimal("0")
        if physical_material_gate:
            # Unknown BOM/routing is a data problem, not proof of a physical
            # shortage. Only a missing purchased component is a red blocker.
            reasons_by_line[int(line.queue_line_id)].update(
                blocker.reason for blocker in blockers_by_line[int(line.queue_line_id)]
                if blocker.reason != "PURCHASED_COMPONENT_SHORTAGE"
            )
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
