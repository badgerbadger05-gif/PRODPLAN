"""Replay the canonical PRODPLAN Ledger/MRP history from a JSON manifest.

This command is intentionally only an orchestrator.  It calls the canonical
Ledger and period-plan services and never implements stock or planning math.
Every persisted generation key is audited before it is reused, which makes a
resume safe while failing closed on a key that belongs to another lineage.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Protocol, Sequence


class ReplayError(RuntimeError):
    """The requested replay cannot safely continue."""


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReplayError(f"{field} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReplayError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayError(f"{field} must include an explicit UTC offset")
    return parsed


def _key(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayError(f"{field} must be a non-empty string")
    result = value.strip()
    if len(result) > 128:
        raise ReplayError(f"{field} must not exceed 128 characters")
    return result


@dataclass(frozen=True)
class PlanReplay:
    plan_id: int
    cutoff: datetime
    physical_key: str | None
    obligation_key: str


@dataclass(frozen=True)
class ReplayManifest:
    opening_at: datetime
    replay_from: datetime
    bootstrap_cutoff: datetime
    bootstrap_key: str
    material_custody_baseline_cells: tuple[dict[str, Any], ...]
    required_assembly_item_codes: tuple[str, ...]
    plans: tuple[PlanReplay, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReplayManifest":
        allowed = {
            "opening_at",
            "replay_from",
            "bootstrap_cutoff",
            "bootstrap_key",
            "material_custody_baseline_cells",
            "required_assembly_item_codes",
            "plans",
        }
        extra = set(raw).difference(allowed)
        missing = allowed.difference(raw)
        if missing:
            raise ReplayError(f"manifest is missing fields: {', '.join(sorted(missing))}")
        if extra:
            raise ReplayError(f"manifest has unknown fields: {', '.join(sorted(extra))}")

        opening_at = _timestamp(raw["opening_at"], "opening_at")
        replay_from = _timestamp(raw["replay_from"], "replay_from")
        bootstrap_cutoff = _timestamp(raw["bootstrap_cutoff"], "bootstrap_cutoff")
        bootstrap_key = _key(raw["bootstrap_key"], "bootstrap_key")
        custody_cells_raw = raw["material_custody_baseline_cells"]
        if not isinstance(custody_cells_raw, list) or any(
            not isinstance(value, Mapping) for value in custody_cells_raw
        ):
            raise ReplayError(
                "material_custody_baseline_cells must be an array of objects"
            )
        custody_cells = tuple(dict(value) for value in custody_cells_raw)
        required_codes_raw = raw["required_assembly_item_codes"]
        if not isinstance(required_codes_raw, list) or not required_codes_raw:
            raise ReplayError("required_assembly_item_codes must be a non-empty array")
        required_codes = tuple(
            _key(value, f"required_assembly_item_codes[{index}]")
            for index, value in enumerate(required_codes_raw)
        )
        if len(set(required_codes)) != len(required_codes):
            raise ReplayError("required_assembly_item_codes must be unique")
        if not opening_at <= replay_from <= bootstrap_cutoff:
            raise ReplayError(
                "timestamps must satisfy opening_at <= replay_from <= bootstrap_cutoff"
            )

        plan_rows = raw["plans"]
        if not isinstance(plan_rows, list) or not plan_rows:
            raise ReplayError("plans must be a non-empty array")
        plans: list[PlanReplay] = []
        for index, row in enumerate(plan_rows):
            field = f"plans[{index}]"
            if not isinstance(row, Mapping):
                raise ReplayError(f"{field} must be an object")
            row_allowed = {"plan_id", "cutoff", "physical_key", "obligation_key"}
            row_extra = set(row).difference(row_allowed)
            row_missing = {"plan_id", "cutoff", "obligation_key"}.difference(row)
            if row_missing:
                raise ReplayError(
                    f"{field} is missing fields: {', '.join(sorted(row_missing))}"
                )
            if row_extra:
                raise ReplayError(
                    f"{field} has unknown fields: {', '.join(sorted(row_extra))}"
                )
            plan_id = row["plan_id"]
            if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id <= 0:
                raise ReplayError(f"{field}.plan_id must be a positive integer")
            physical_value = row.get("physical_key")
            physical_key = (
                None
                if physical_value is None
                else _key(physical_value, f"{field}.physical_key")
            )
            plans.append(
                PlanReplay(
                    plan_id=plan_id,
                    cutoff=_timestamp(row["cutoff"], f"{field}.cutoff"),
                    physical_key=physical_key,
                    obligation_key=_key(
                        row["obligation_key"], f"{field}.obligation_key"
                    ),
                )
            )

        if plans[0].cutoff != bootstrap_cutoff or plans[0].physical_key is not None:
            raise ReplayError(
                "the first plan must use bootstrap_cutoff and omit physical_key"
            )
        previous = bootstrap_cutoff
        for index, plan in enumerate(plans[1:], start=1):
            if plan.physical_key is None:
                raise ReplayError(f"plans[{index}].physical_key is required")
            if plan.cutoff <= previous:
                raise ReplayError("plan cutoffs must be strictly increasing")
            previous = plan.cutoff

        plan_ids = [plan.plan_id for plan in plans]
        if len(set(plan_ids)) != len(plan_ids):
            raise ReplayError("plan_id values must be unique")
        keys = [bootstrap_key]
        keys.extend(plan.obligation_key for plan in plans)
        keys.extend(plan.physical_key for plan in plans if plan.physical_key is not None)
        if len(set(keys)) != len(keys):
            raise ReplayError("all generation keys in the manifest must be unique")
        return cls(
            opening_at=opening_at,
            replay_from=replay_from,
            bootstrap_cutoff=bootstrap_cutoff,
            bootstrap_key=bootstrap_key,
            material_custody_baseline_cells=custody_cells,
            required_assembly_item_codes=required_codes,
            plans=tuple(plans),
        )


def load_manifest(path: Path) -> ReplayManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReplayError(f"cannot read manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReplayError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ReplayError("manifest root must be an object")
    return ReplayManifest.from_mapping(raw)


@dataclass(frozen=True)
class GenerationState:
    generation_id: int
    key: str
    status: str
    cutoff: datetime
    parent_generation_id: int | None
    historical_from: datetime | None = None
    replay_from: datetime | None = None


class ReplayRuntime(Protocol):
    def preflight_assembly_rates(self, item_codes: Sequence[str]) -> None: ...

    def preflight_planning_pools(
        self, *, max_off_contour_percent: float, allow_off_contour: bool
    ) -> Mapping[str, Any]: ...

    def preflight_plan_statuses(
        self, plan_ids: Sequence[int], *, allow_excluded_plans: bool
    ) -> Mapping[str, Any]: ...

    def generation(self, key: str) -> GenerationState | None: ...

    def current_generation_id(self) -> int | None: ...

    def bootstrap(self, manifest: ReplayManifest) -> GenerationState: ...

    def import_once(self, generation_id: int) -> bool: ...

    def balance_is_valid(self, generation_id: int) -> bool: ...

    def verify_balance(self, generation_id: int) -> bool: ...

    def initialize_custody_baseline(
        self,
        generation_id: int,
        cells: Sequence[dict[str, Any]],
        observed_at: datetime,
    ) -> None: ...

    def accept_bootstrap(self, generation_id: int, replay_from: datetime) -> None: ...

    def fixed_snapshot_exists(self, generation_id: int, plan_id: int) -> bool: ...

    def physical_refresh(self, cutoff: datetime, key: str) -> None: ...

    def create_snapshot(self, plan_id: int, key: str) -> None: ...

    def commit(self) -> None: ...

    def fix_plan(self, plan_id: int) -> None: ...

    def rollback(self) -> None: ...


def _same_instant(left: datetime, right: datetime) -> bool:
    return left.timestamp() == right.timestamp()


def _require_generation(
    runtime: ReplayRuntime,
    *,
    key: str,
    cutoff: datetime,
    parent_id: int | None,
    plan_id: int | None = None,
) -> GenerationState | None:
    state = runtime.generation(key)
    if state is None:
        return None
    if not _same_instant(state.cutoff, cutoff):
        raise ReplayError(f"generation key {key!r} exists with a different cutoff")
    if state.parent_generation_id != parent_id:
        raise ReplayError(f"generation key {key!r} exists in a different lineage")
    if state.status not in {"building", "accepted"}:
        raise ReplayError(
            f"generation key {key!r} has non-retryable status {state.status!r}"
        )
    if state.status == "accepted" and plan_id is not None:
        if not runtime.fixed_snapshot_exists(state.generation_id, plan_id):
            raise ReplayError(
                f"accepted generation {key!r} has no unique fixed snapshot "
                f"for plan {plan_id}"
            )
    return state


def run_preflight(
    runtime: ReplayRuntime,
    manifest: ReplayManifest,
    *,
    max_off_contour_percent: float,
    allow_off_contour: bool,
    allow_excluded_plans: bool,
) -> dict[str, Any]:
    """Run every read-only gate and return one report section per gate."""
    runtime.preflight_assembly_rates(manifest.required_assembly_item_codes)
    pools = runtime.preflight_planning_pools(
        max_off_contour_percent=max_off_contour_percent,
        allow_off_contour=allow_off_contour,
    )
    plans = runtime.preflight_plan_statuses(
        tuple(plan.plan_id for plan in manifest.plans),
        allow_excluded_plans=allow_excluded_plans,
    )
    return {
        "required_assembly_item_codes": list(manifest.required_assembly_item_codes),
        "planning_pools": dict(pools or {}),
        "plans": dict(plans or {}),
    }


def replay_history(
    runtime: ReplayRuntime,
    manifest: ReplayManifest,
    *,
    max_import_iterations: int,
    max_off_contour_percent: float = 20.0,
    allow_off_contour: bool = False,
    allow_excluded_plans: bool = False,
) -> dict[str, Any]:
    if max_import_iterations <= 0:
        raise ReplayError("max_import_iterations must be positive")
    preflight = run_preflight(
        runtime,
        manifest,
        max_off_contour_percent=max_off_contour_percent,
        allow_off_contour=allow_off_contour,
        allow_excluded_plans=allow_excluded_plans,
    )

    bootstrap = runtime.generation(manifest.bootstrap_key)
    if bootstrap is None:
        if runtime.current_generation_id() is not None:
            raise ReplayError("bootstrap requires an empty planning-truth pointer")
        bootstrap = runtime.bootstrap(manifest)
    if (
        not _same_instant(bootstrap.cutoff, manifest.bootstrap_cutoff)
        or bootstrap.parent_generation_id is not None
        or bootstrap.historical_from is None
        or not _same_instant(bootstrap.historical_from, manifest.opening_at)
        or bootstrap.replay_from is None
        or not _same_instant(bootstrap.replay_from, manifest.replay_from)
    ):
        raise ReplayError("bootstrap key exists with different sealed lineage")
    if bootstrap.status not in {"building", "accepted"}:
        raise ReplayError(f"bootstrap has non-retryable status {bootstrap.status!r}")

    if bootstrap.status == "building":
        complete = False
        for _ in range(max_import_iterations):
            complete = runtime.import_once(bootstrap.generation_id)
            if complete:
                break
        if not complete:
            raise ReplayError(
                "historical import did not complete within max_import_iterations"
            )
        if not runtime.balance_is_valid(bootstrap.generation_id):
            if not runtime.verify_balance(bootstrap.generation_id):
                raise ReplayError("historical balance convergence failed")
        runtime.initialize_custody_baseline(
            bootstrap.generation_id,
            manifest.material_custody_baseline_cells,
            manifest.bootstrap_cutoff,
        )
        runtime.accept_bootstrap(bootstrap.generation_id, manifest.replay_from)
        bootstrap = runtime.generation(manifest.bootstrap_key)
        if bootstrap is None or bootstrap.status != "accepted":
            raise ReplayError("bootstrap acceptance did not publish the generation")
    elif not runtime.balance_is_valid(bootstrap.generation_id):
        raise ReplayError("accepted bootstrap lacks valid balance convergence evidence")

    expected_parent_id = bootstrap.generation_id
    completed_plans: list[int] = []
    for index, plan in enumerate(manifest.plans):
        if index:
            assert plan.physical_key is not None
            physical = _require_generation(
                runtime,
                key=plan.physical_key,
                cutoff=plan.cutoff,
                parent_id=expected_parent_id,
            )
            if physical is None or physical.status == "building":
                if runtime.current_generation_id() != expected_parent_id:
                    raise ReplayError(
                        f"cannot build {plan.physical_key!r}: expected parent is not "
                        "the current planning truth"
                    )
                runtime.physical_refresh(plan.cutoff, plan.physical_key)
                physical = _require_generation(
                    runtime,
                    key=plan.physical_key,
                    cutoff=plan.cutoff,
                    parent_id=expected_parent_id,
                )
            if physical is None or physical.status != "accepted":
                raise ReplayError(f"physical refresh {plan.physical_key!r} was not accepted")
            expected_parent_id = physical.generation_id

        obligation = _require_generation(
            runtime,
            key=plan.obligation_key,
            cutoff=plan.cutoff,
            parent_id=expected_parent_id,
            plan_id=plan.plan_id,
        )
        if obligation is None or obligation.status == "building":
            if runtime.current_generation_id() != expected_parent_id:
                raise ReplayError(
                    f"cannot build {plan.obligation_key!r}: expected parent is not "
                    "the current planning truth"
                )
            try:
                runtime.create_snapshot(plan.plan_id, plan.obligation_key)
                runtime.commit()
                runtime.fix_plan(plan.plan_id)
            except Exception:
                runtime.rollback()
                raise
            obligation = _require_generation(
                runtime,
                key=plan.obligation_key,
                cutoff=plan.cutoff,
                parent_id=expected_parent_id,
                plan_id=plan.plan_id,
            )
        if obligation is None or obligation.status != "accepted":
            raise ReplayError(
                f"obligation refresh {plan.obligation_key!r} was not accepted"
            )
        expected_parent_id = obligation.generation_id
        completed_plans.append(plan.plan_id)

    if runtime.current_generation_id() != expected_parent_id:
        raise ReplayError("final accepted generation is not the current planning truth")
    return {
        "status": "complete",
        "final_generation_id": expected_parent_id,
        "plans": completed_plans,
        "preflight": preflight,
    }


class DatabaseRuntime:
    """Production adapter around canonical services (no business calculations)."""

    def __init__(
        self,
        db: Any,
        *,
        window_hours: int,
        page_size: int,
        max_pages_per_window: int,
    ) -> None:
        self.db = db
        self.window_hours = window_hours
        self.page_size = page_size
        self.max_pages_per_window = max_pages_per_window

    def preflight_assembly_rates(self, item_codes: Sequence[str]) -> None:
        """Read-only gate: every required SKU has one usable resource rate."""
        from app import models

        rows = (
            self.db.query(
                models.Item.item_code,
                models.AssemblyRate.id,
                models.AssemblyRate.qty_per_capacity,
                models.ProductionResource.resource_id,
            )
            .outerjoin(
                models.AssemblyRate,
                models.AssemblyRate.item_id == models.Item.item_id,
            )
            .outerjoin(
                models.ProductionResource,
                models.ProductionResource.resource_id
                == models.AssemblyRate.resource_id,
            )
            .filter(models.Item.item_code.in_(tuple(item_codes)))
            .all()
        )
        by_code: dict[str, list[Any]] = {code: [] for code in item_codes}
        found_items: set[str] = set()
        for item_code, rate_id, qty, resource_id in rows:
            code = str(item_code)
            found_items.add(code)
            if (
                rate_id is not None
                and resource_id is not None
                and qty is not None
                and qty > 0
            ):
                by_code[code].append(rate_id)
        ambiguous = [code for code in item_codes if len(by_code[code]) > 1]
        problems = []
        if ambiguous:
            problems.append(f"ambiguous assembly rates: {', '.join(ambiguous)}")
        if problems:
            raise ReplayError("assembly-rate preflight failed; " + "; ".join(problems))

    def preflight_planning_pools(
        self,
        *,
        max_off_contour_percent: float,
        allow_off_contour: bool,
    ) -> dict[str, Any]:
        """Read-only gate for supplier and WIP future-supply qualification.

        The contour itself must be non-empty (hard fail).  Every destination a
        capture boundary will actually evaluate is measured against it, so the
        operator sees the expected ``rejected`` share *before* the destructive
        clear instead of discovering it as zero coverage afterwards.
        """
        from app import models
        from app.services.planning_pool_resolver import (
            PlanningPoolConfigurationError,
            resolve_planning_pool_by_warehouse,
        )
        from app.services.supplier_order_status import SupplyPhase, phase_for_state

        try:
            mapping = resolve_planning_pool_by_warehouse(self.db)
        except PlanningPoolConfigurationError as exc:
            raise ReplayError(f"planning-pool preflight failed; {exc}") from exc

        # Same sources the capture boundaries read: the 1C supplier-order mirror
        # and every ProductionProduct line.
        evaluated: list[tuple[str, str]] = []
        not_evaluated = 0
        for destination, state_name, deleted in (
            self.db.query(
                models.SupplierOrderItem.destination_warehouse_ref1c,
                models.SupplierOrder.order_state_name,
                models.SupplierOrder.deletion_mark,
            )
            .join(
                models.SupplierOrder,
                models.SupplierOrder.order_id == models.SupplierOrderItem.order_id,
            )
            .all()
        ):
            # Deleted and non-goods phases are rejected before the pool check,
            # so they cannot contribute an off-contour destination.
            if bool(deleted) or phase_for_state(state_name) not in {
                SupplyPhase.IN_TRANSIT,
                SupplyPhase.IN_STOCK,
            }:
                not_evaluated += 1
                continue
            evaluated.append(("supplier_order", str(destination or "").strip()))
        for (destination,) in self.db.query(
            models.ProductionProduct.destination_warehouse_ref1c
        ).all():
            evaluated.append(("wip_order", str(destination or "").strip()))

        names = {
            str(ref or "").strip(): str(name or "")
            for ref, name in self.db.query(
                models.StockWarehouse.warehouse_ref1c,
                models.StockWarehouse.warehouse_name,
            ).all()
        }
        off_by_warehouse: dict[str, dict[str, Any]] = {}
        in_contour = 0
        off_contour = 0
        not_stamped = 0
        for supply_kind, destination in evaluated:
            if not destination:
                not_stamped += 1
                continue
            if mapping.get(destination):
                in_contour += 1
                continue
            off_contour += 1
            bucket = off_by_warehouse.setdefault(
                destination,
                {
                    "warehouse_ref": destination,
                    "warehouse_name": names.get(destination, ""),
                    "rows": 0,
                    "supplier_order_rows": 0,
                    "wip_order_rows": 0,
                },
            )
            bucket["rows"] += 1
            bucket[f"{supply_kind}_rows"] += 1

        mapped = in_contour + off_contour
        percent = (100.0 * off_contour / mapped) if mapped else 0.0
        details = ", ".join(
            f"{row['warehouse_ref']} ({row['warehouse_name'] or 'unknown name'}): "
            f"{row['rows']} rows"
            for row in sorted(
                off_by_warehouse.values(), key=lambda row: (-row["rows"], row["warehouse_ref"])
            )
        )
        warnings: list[str] = []
        if not_stamped:
            warnings.append(
                f"{not_stamped} rows carry no destination warehouse and stay rejected"
            )
        if off_contour:
            warnings.append(
                f"{off_contour} of {mapped} rows ({percent:.1f}%) target "
                f"{len(off_by_warehouse)} warehouse(s) outside the live planning "
                f"contour and stay rejected: {details}"
            )
        report: dict[str, Any] = {
            "contour_warehouses": len(mapping),
            "rows_total": len(evaluated) + not_evaluated,
            "rows_not_evaluated": not_evaluated,
            "rows_destination_not_stamped": not_stamped,
            "rows_in_contour": in_contour,
            "rows_off_contour": off_contour,
            "off_contour_percent": round(percent, 3),
            "max_off_contour_percent": float(max_off_contour_percent),
            "allow_off_contour": bool(allow_off_contour),
            "off_contour_by_warehouse": sorted(
                off_by_warehouse.values(),
                key=lambda row: (-row["rows"], row["warehouse_ref"]),
            ),
            "warnings": warnings,
        }
        if off_contour and percent > max_off_contour_percent and not allow_off_contour:
            raise ReplayError(
                "planning-pool preflight failed; off-contour destinations are "
                f"{percent:.1f}% of {mapped} rows (threshold "
                f"{float(max_off_contour_percent):.1f}%): {details}; rerun with "
                "--allow-off-contour to accept that coverage loss"
            )
        return report

    def preflight_plan_statuses(
        self,
        plan_ids: Sequence[int],
        *,
        allow_excluded_plans: bool,
    ) -> dict[str, Any]:
        """Read-only gate: the replay range holds exactly the manifest plans.

        ``create_mrp_snapshot_from_period_plan`` only accepts a ``fixed`` plan,
        so a ``draft``/``closed`` plan would abort mid-replay.  A live plan
        inside the range but outside the manifest would silently lose its
        obligations, so it must be acknowledged instead of dropped in silence.
        """
        from app import models

        wanted = [int(value) for value in plan_ids]
        if not wanted:
            raise ReplayError("plan-status preflight failed; manifest has no plans")
        rows = (
            self.db.query(
                models.ProductionPlanHeader.id,
                models.ProductionPlanHeader.name,
                models.ProductionPlanHeader.status,
            )
            .filter(
                models.ProductionPlanHeader.id >= min(wanted),
                models.ProductionPlanHeader.id <= max(wanted),
            )
            .order_by(models.ProductionPlanHeader.id.asc())
            .all()
        )
        live = {
            int(plan_id): (str(name or ""), str(status or ""))
            for plan_id, name, status in rows
        }
        selected = set(wanted)
        missing = [plan_id for plan_id in wanted if plan_id not in live]
        not_fixed = [
            {"plan_id": plan_id, "name": live[plan_id][0], "status": live[plan_id][1]}
            for plan_id in wanted
            if plan_id in live and live[plan_id][1] != "fixed"
        ]
        excluded = [
            {"plan_id": plan_id, "name": name, "status": status}
            for plan_id, (name, status) in sorted(live.items())
            if plan_id not in selected
        ]
        excluded_live = [row for row in excluded if row["status"] != "closed"]
        warnings: list[str] = []
        if excluded:
            warnings.append(
                "plans inside the replay range are excluded from the manifest: "
                + ", ".join(
                    f"{row['plan_id']}={row['status']}" for row in excluded
                )
            )
        report: dict[str, Any] = {
            "plan_id_range": [min(wanted), max(wanted)],
            "manifest_plans": wanted,
            "plans_in_range": len(live),
            "missing_plans": missing,
            "not_fixed_plans": not_fixed,
            "excluded_plans": excluded,
            "excluded_live_plans": [row["plan_id"] for row in excluded_live],
            "allow_excluded_plans": bool(allow_excluded_plans),
            "warnings": warnings,
        }
        problems = []
        if missing:
            problems.append(
                "manifest plans do not exist: "
                + ", ".join(str(plan_id) for plan_id in missing)
            )
        if not_fixed:
            problems.append(
                "manifest plans are not fixed: "
                + ", ".join(f"{row['plan_id']}={row['status']}" for row in not_fixed)
            )
        if excluded_live and not allow_excluded_plans:
            problems.append(
                "live plans inside the replay range are absent from the manifest: "
                + ", ".join(
                    f"{row['plan_id']} ({row['name'] or 'unnamed'})={row['status']}"
                    for row in excluded_live
                )
                + "; rerun with --allow-excluded-plans to drop their obligations"
            )
        if problems:
            raise ReplayError("plan-status preflight failed; " + "; ".join(problems))
        return report

    def generation(self, key: str) -> GenerationState | None:
        from app import models

        row = (
            self.db.query(models.LedgerGeneration)
            .filter(models.LedgerGeneration.generation_key == key)
            .one_or_none()
        )
        if row is None:
            return None
        marks = dict(row.source_watermarks or {})
        parent = marks.get("parent_generation_id")
        historical = marks.get("historical_from_exclusive")
        replay_from = marks.get("replay_from")
        return GenerationState(
            generation_id=int(row.id),
            key=str(row.generation_key),
            status=str(row.status),
            cutoff=row.cutoff,
            parent_generation_id=int(parent) if parent is not None else None,
            historical_from=datetime.fromisoformat(historical) if historical else None,
            replay_from=datetime.fromisoformat(replay_from) if replay_from else None,
        )

    def current_generation_id(self) -> int | None:
        from app import models

        pointer = self.db.get(models.PlanningTruthState, 1)
        if pointer is None or pointer.current_generation_id is None:
            return None
        return int(pointer.current_generation_id)

    def bootstrap(self, manifest: ReplayManifest) -> GenerationState:
        from app.routers.item_ledger_admin import (
            HistoricalBootstrapRequest,
            bootstrap_historical_generation,
        )

        bootstrap_historical_generation(
            HistoricalBootstrapRequest(
                generation_key=manifest.bootstrap_key,
                opening_at=manifest.opening_at,
                replay_from=manifest.replay_from,
                cutoff=manifest.bootstrap_cutoff,
            ),
            self.db,
        )
        state = self.generation(manifest.bootstrap_key)
        if state is None:
            raise ReplayError("bootstrap service did not create a generation")
        return state

    def import_once(self, generation_id: int) -> bool:
        from app.routers.item_ledger_admin import (
            HistoricalImportRequest,
            import_historical_generation,
        )

        result = import_historical_generation(
            generation_id,
            HistoricalImportRequest(
                max_windows=1,
                window_hours=self.window_hours,
                page_size=self.page_size,
                max_pages_per_window=self.max_pages_per_window,
                pause_seconds=0,
            ),
            self.db,
        )
        return bool(result["complete"])

    def balance_is_valid(self, generation_id: int) -> bool:
        from app import models

        row = self.db.get(models.LedgerGeneration, generation_id)
        convergence = dict(row.source_watermarks or {}).get("balance_convergence", {})
        return convergence.get("valid") is True

    def verify_balance(self, generation_id: int) -> bool:
        from app.routers.item_ledger_admin import verify_historical_generation_balance

        return bool(verify_historical_generation_balance(generation_id, self.db)["valid"])

    def accept_bootstrap(self, generation_id: int, replay_from: datetime) -> None:
        from app.routers.item_ledger_admin import GenerationAcceptRequest, accept_generation

        accept_generation(
            GenerationAcceptRequest(
                generation_id=generation_id,
                replay_from=replay_from,
                explicit_empty_physical=False,
                expected_parent_generation_id=None,
            ),
            self.db,
        )

    def initialize_custody_baseline(
        self,
        generation_id: int,
        cells: Sequence[dict[str, Any]],
        observed_at: datetime,
    ) -> None:
        from app import models
        from app.services.production_material_custody_projection import (
            initialize_material_custody_baseline,
        )

        existing = self.db.get(
            models.ProductionMaterialCustodyProjectionManifest,
            int(generation_id),
        )
        if existing is not None:
            raise ReplayError(
                "bootstrap custody baseline already exists before acceptance"
            )
        initialize_material_custody_baseline(
            self.db,
            ledger_generation_id=int(generation_id),
            cells=cells,
            observed_at=observed_at,
        )

    def fixed_snapshot_exists(self, generation_id: int, plan_id: int) -> bool:
        from app import models

        rows = (
            self.db.query(models.PlanningRun)
            .filter(
                models.PlanningRun.ledger_generation_id == generation_id,
                models.PlanningRun.source_plan_id == plan_id,
                models.PlanningRun.status == "FIXED_SNAPSHOT",
            )
            .all()
        )
        return len(rows) == 1

    def physical_refresh(self, cutoff: datetime, key: str) -> None:
        from app.services.sync_orchestrator import _run_physical_refresh_job

        result = _run_physical_refresh_job(self.db, cutoff, key)
        if not result.get("published"):
            raise ReplayError(f"physical refresh {key!r} was not published")

    def create_snapshot(self, plan_id: int, key: str) -> None:
        from app.services.period_plan_service import create_mrp_snapshot_from_period_plan

        result = create_mrp_snapshot_from_period_plan(
            self.db,
            plan_id,
            generation_key=key,
            started_by="historical-replay-cli",
            allow_stale_parent=True,
        )
        if not result.get("published") and not result.get("immutable"):
            raise ReplayError(f"obligation refresh {key!r} was not published")

    def commit(self) -> None:
        self.db.commit()

    def fix_plan(self, plan_id: int) -> None:
        from app.services.period_plan_service import fix_period_plan

        fix_period_plan(self.db, plan_id, fixed_by="historical-replay-cli")

    def rollback(self) -> None:
        self.db.rollback()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="path to replay JSON manifest")
    parser.add_argument("--max-import-iterations", type=int, default=10_000)
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages-per-window", type=int, default=10_000)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="run every read-only gate and report without replay mutations",
    )
    parser.add_argument(
        "--max-off-contour-percent",
        type=float,
        default=20.0,
        help="fail when more of the measured supply rows target warehouses "
        "outside the live planning contour",
    )
    parser.add_argument(
        "--allow-off-contour",
        action="store_true",
        help="acknowledge the measured off-contour share above the threshold",
    )
    parser.add_argument(
        "--allow-excluded-plans",
        action="store_true",
        help="acknowledge live plans inside the replay range that the manifest omits",
    )
    return parser


def _backend_dir() -> Path:
    """Locate the FastAPI source without assuming a machine-specific root."""
    candidates: list[Path] = []
    configured = os.environ.get("PRODPLAN_BACKEND_DIR")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        (
            Path(__file__).resolve().parents[1] / "backend",
            Path.cwd(),
        )
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "app" / "database.py").is_file():
            return resolved
    checked = ", ".join(str(path) for path in candidates)
    raise ReplayError(
        "cannot locate backend source; set PRODPLAN_BACKEND_DIR "
        f"(checked: {checked})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.window_hours < 1 or args.window_hours > 168:
        raise ReplayError("window_hours must be between 1 and 168")
    if args.page_size < 1 or args.page_size > 5000:
        raise ReplayError("page_size must be between 1 and 5000")
    if args.max_pages_per_window < 1 or args.max_pages_per_window > 100_000:
        raise ReplayError("max_pages_per_window must be between 1 and 100000")
    if not 0.0 <= args.max_off_contour_percent <= 100.0:
        raise ReplayError("max_off_contour_percent must be between 0 and 100")

    manifest = load_manifest(args.manifest)
    backend = _backend_dir()
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        runtime = DatabaseRuntime(
            db,
            window_hours=args.window_hours,
            page_size=args.page_size,
            max_pages_per_window=args.max_pages_per_window,
        )
        if args.preflight_only:
            result = {
                "status": "preflight-ok",
                "preflight": run_preflight(
                    runtime,
                    manifest,
                    max_off_contour_percent=args.max_off_contour_percent,
                    allow_off_contour=args.allow_off_contour,
                    allow_excluded_plans=args.allow_excluded_plans,
                ),
            }
        else:
            result = replay_history(
                runtime,
                manifest,
                max_import_iterations=args.max_import_iterations,
                max_off_contour_percent=args.max_off_contour_percent,
                allow_off_contour=args.allow_off_contour,
                allow_excluded_plans=args.allow_excluded_plans,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReplayError as exc:
        print(f"replay refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
