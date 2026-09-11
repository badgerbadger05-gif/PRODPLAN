"""Persisted read boundary for the production-control journal.

The expensive journal projection is built while a Ledger generation is still
``building``.  Public GETs only page immutable rows belonging to the current
accepted generation; they never rebuild the projection or fall back to live
operational tables.

Mutation services intentionally remain separate.  They validate and write
commands against normalized state, while a later worker publication produces
the next read model.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fastapi.encoders import jsonable_encoder
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app import models
from app.services.bom_specification_resolver import BomSpecificationResolver
from app.services.production_control_printing import build_route_sheet_snapshot_payloads
from app.services.production_control_live_launch import (
    overlay_execution_state,
    overlay_launch_facts,
    route_sheets_after_cutoff,
)
from app.services.item_ledger.future_supply_capture import verify_future_supply_capture
from app.services.planning_truth import (
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_PRODUCTION_CONTROL_JOURNAL,
    CAPABILITY_RESERVATION_REPLAY,
    PlanningTruthReadiness,
    PlanningTruthUnavailable,
    get_truth_state,
)
from app.services.production_control_journal import (
    STATUS_FILTER_GROUPS,
    list_journal,
    list_make_proposals,
)
from app.services.production_control_common import DONE_STATE_KEY


CONSUMER = "production_control_journal"
SNAPSHOT_KEY = "journal:v1"
ROW_KIND = "production_order"
PROPOSAL_ROW_KIND = "production_proposal"
ROW_KINDS = (ROW_KIND, PROPOSAL_ROW_KIND)
REQUIRED = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_PRODUCTION_CONTROL_JOURNAL,
)
_PAGE_SIZE = 500


class ProductionControlJournalUnavailable(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(detail["reason"])

    def as_dict(self) -> dict[str, Any]:
        return dict(self.detail)


class RouteSheetSnapshotUnavailable(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(detail["reason"])

    def as_dict(self) -> dict[str, Any]:
        return dict(self.detail)


class ProductionControlJournalPromotionError(RuntimeError):
    """A building journal candidate cannot be exposed as accepted truth."""


def _public_journal_row(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    row.pop("material_coverage_snapshot", None)
    row.pop("_route_sheet_snapshot", None)
    return row


def _drum_readiness_pull_by_run_item(
    db: Session,
    ledger_generation_id: int,
) -> dict[tuple[int, int], dict[str, Any]]:
    """Collapse per-tile make actions into journal-ready pull provenance."""
    slot_rows = (
        db.query(models.DrumSlot, models.AssemblyQueueLine)
        .join(models.DrumSchedule, models.DrumSchedule.id == models.DrumSlot.drum_schedule_id)
        .join(
            models.AssemblyQueueLine,
            models.AssemblyQueueLine.id == models.DrumSlot.assembly_queue_line_id,
        )
        .filter(models.DrumSchedule.ledger_generation_id == int(ledger_generation_id))
        .order_by(models.DrumSlot.slot_date, models.DrumSlot.id)
        .all()
    )
    result: dict[tuple[int, int], dict[str, Any]] = {}
    for slot, line in slot_rows:
        protected = {
            "drum_slot_id": int(slot.id),
            "root_item_id": int(slot.item_id),
            "slot_date": slot.slot_date.isoformat(),
            "slot_qty": str(slot.slot_qty),
            "readiness_phase": str(slot.readiness_phase),
        }
        for action in list(slot.action_manifest or []):
            if str(action.get("action_kind") or "") not in {"make", "rework", "kitting"}:
                continue
            item_id = int(action["item_id"])
            key = (int(line.planning_run_id), item_id)
            entry = result.setdefault(key, {
                "readiness_required_qty": 0.0,
                "readiness_need_date": None,
                "readiness_action_date": None,
                "readiness_priority_key": str(line.sort_key),
                "protected_drum_slots": [],
            })
            entry["readiness_required_qty"] += float(action.get("qty") or 0)
            action_date = action.get("available_date")
            if action_date and (
                entry["readiness_action_date"] is None
                or str(action_date) < str(entry["readiness_action_date"])
            ):
                entry["readiness_action_date"] = str(action_date)
            if str(line.sort_key) < str(entry["readiness_priority_key"]):
                entry["readiness_priority_key"] = str(line.sort_key)
            known_ids = {row["drum_slot_id"] for row in entry["protected_drum_slots"]}
            if protected["drum_slot_id"] not in known_ids:
                entry["protected_drum_slots"].append(protected)
            need_date = protected["slot_date"]
            if entry["readiness_need_date"] is None or need_date < entry["readiness_need_date"]:
                entry["readiness_need_date"] = need_date
    return result


def _route_sheet_payload_value(row: Mapping[str, Any], *, product_id: int) -> dict[str, Any]:
    route_payload = row.get("_route_sheet_snapshot")
    if not isinstance(route_payload, dict):
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate route-sheet snapshot is malformed"
        )

    try:
        version = int(route_payload["version"])
        if version <= 0:
            raise TypeError

        anchor_product_id = int(route_payload["anchor_product_id"])
        sheet = route_payload["sheet"]
        if not isinstance(sheet, dict):
            raise TypeError

        chain = sheet.get("chain") or {}
        if not isinstance(chain, dict):
            raise TypeError

        sheet_product_id = int(sheet["product_id"])
        if sheet_product_id <= 0 or anchor_product_id <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate route-sheet snapshot is malformed"
        ) from exc

    components = sheet.get("components")
    if not isinstance(components, list):
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate route-sheet snapshot is malformed"
        )
    for component in components:
        if not isinstance(component, Mapping):
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate route-sheet snapshot is malformed"
            )
        try:
            required_qty = float(component.get("required_qty"))
            qty_per_unit = float(component.get("qty_per_unit"))
        except (TypeError, ValueError) as exc:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate route-sheet snapshot is malformed"
            ) from exc
        if required_qty < 0 or qty_per_unit < 0:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate route-sheet snapshot is malformed"
            )

    if chain:
        try:
            weld_product_id = int(chain.get("weld_product_id"))
            weld_qty = float(chain.get("weld_qty"))
        except (TypeError, ValueError) as exc:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate route-sheet snapshot is malformed"
            ) from exc
        if weld_qty < 0 or weld_product_id <= 0:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate route-sheet snapshot is malformed"
            )
        if product_id not in {anchor_product_id, weld_product_id}:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate route-sheet snapshot is malformed"
            )
    elif anchor_product_id != product_id:
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate route-sheet snapshot is malformed"
        )

    try:
        remaining_qty = float(sheet.get("remaining_qty"))
    except (TypeError, ValueError) as exc:
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate route-sheet snapshot is malformed"
        ) from exc
    if remaining_qty < 0:
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate route-sheet snapshot is malformed"
        )

    return dict(deepcopy(route_payload))


def list_root_product_options(
    db: Session,
) -> list[dict[str, Any]]:
    # Runtime reads use the compact accepted current owner.  The immutable
    # snapshot path remains below only for explicit migration/archive tools.
    from app.services.item_ledger.current_execution import (
        CurrentExecutionUnavailable,
        require_current_execution_scope,
    )
    try:
        manifest = require_current_execution_scope(
            db,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )
    except CurrentExecutionUnavailable as exc:
        raise _unavailable(db, str(exc)) from exc
    options = (manifest.summary or {}).get("root_product_options")
    if options is not None:
        if not isinstance(options, list) or any(not isinstance(row, dict) for row in options):
            raise _unavailable(db, "accepted production-control root options are malformed")
        return [dict(row) for row in options]
    raise _unavailable(db, "accepted production-control root options are missing")



def _root_product_options(
    db: Session,
    roots_by_product: Mapping[object, set[int]],
) -> list[dict[str, Any]]:
    root_ids = sorted({root_id for values in roots_by_product.values() for root_id in values})
    if not root_ids:
        return []
    items = db.query(models.Item).filter(models.Item.item_id.in_(root_ids)).all()
    by_id = {int(item.item_id): item for item in items}
    if set(by_id) != set(root_ids):
        raise ValueError("production-control root product display identity is missing")
    options = [
        {
            "item_id": item_id,
            "item_name": str(by_id[item_id].item_name or ""),
            "item_article": by_id[item_id].item_article,
            "item_code": by_id[item_id].item_code,
        }
        for item_id in root_ids
    ]
    options.sort(
        key=lambda row: (
            str(row.get("item_article") or row.get("item_name") or row.get("item_code") or ""),
            str(row.get("item_name") or ""),
            str(row.get("item_code") or ""),
            int(row["item_id"]),
        )
    )
    return options


def _generation_cutoff(db: Session, generation_id: int | None):
    """Cutoff поколения, которому принадлежит снимок.

    Граница между «уже в снимке» и «появилось позже» — только этот cutoff.
    """
    if generation_id is None:
        return None
    generation = db.get(models.LedgerGeneration, int(generation_id))
    return generation.cutoff if generation is not None else None


def _unavailable(
    db: Session,
    reason: str,
    truth: Mapping[str, Any] | None = None,
) -> ProductionControlJournalUnavailable:
    state = get_truth_state(db)
    detail: dict[str, Any] = {
        "code": "production_control_journal_current_unavailable",
        "consumer": CONSUMER,
        "status": "unavailable",
        "truth_status": state.status,
        "ledger_generation": state.generation_id,
        "cutoff": state.cutoff.isoformat() if state.cutoff else None,
        "reason": reason,
    }
    if truth is not None:
        detail["truth"] = jsonable_encoder(dict(truth))
    return ProductionControlJournalUnavailable(detail)


def _route_sheet_unavailable(
    db: Session,
    reason: str,
    truth: Mapping[str, Any] | None = None,
) -> RouteSheetSnapshotUnavailable:
    state = get_truth_state(db)
    detail: dict[str, Any] = {
        "code": "route_sheet_snapshot_unavailable",
        "consumer": CONSUMER,
        "status": "unavailable",
        "truth_status": state.status,
        "ledger_generation": state.generation_id,
        "cutoff": state.cutoff.isoformat() if state.cutoff else None,
        "reason": reason,
    }
    if truth is not None:
        detail["truth"] = jsonable_encoder(dict(truth))
    return RouteSheetSnapshotUnavailable(detail)


def _candidate_truth(generation: models.LedgerGeneration) -> PlanningTruthReadiness:
    return PlanningTruthReadiness(
        truth_status="building",
        ready=False,
        ledger_generation=int(generation.id),
        generation_key=str(generation.generation_key or ""),
        cutoff=generation.cutoff,
        source_watermarks=dict(generation.source_watermarks or {}),
        capabilities={
            str(name): bool(enabled)
            for name, enabled in dict(generation.capabilities or {}).items()
        },
        algorithm_version=generation.algorithm_version,
        replay_version=generation.replay_version,
        reason="unpublished production-control journal candidate",
        accepted_at=None,
    )


def _build_rows(
    db: Session,
    generation: models.LedgerGeneration,
    accepted_run_ids: Sequence[int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    truth = _candidate_truth(generation)
    run_ids = tuple(sorted({int(value) for value in accepted_run_ids}))
    from app.services.production_control_material_availability import (
        _active_product_ids,
        preview_materials,
    )

    material_coverage_by_product = {
        product_id: preview_materials(
            db,
            product_id,
            ledger_generation_id=int(generation.id),
        )
        for product_id in _active_product_ids(db)
    }
    rows: list[dict[str, Any]] = []
    offset = 0
    total = 0
    latest_run_id: int | None = None
    latest_source_plan_id: int | None = None
    while True:
        page = list_journal(
            db,
            truth=truth,
            _accepted_run_ids_override=run_ids,
            _material_coverage_by_product=material_coverage_by_product,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        if offset == 0:
            total = int(page["total"])
            latest_run_id = page.get("latest_run_id")
            latest_source_plan_id = page.get("latest_source_plan_id")
        page_rows = page.get("rows")
        if not isinstance(page_rows, list):
            raise ValueError("production-control journal builder returned malformed rows")
        rows.extend(dict(row) for row in page_rows)
        if len(rows) >= total:
            break
        if not page_rows:
            raise ValueError("production-control journal builder stopped before total")
        offset += len(page_rows)

    if len(rows) != total:
        raise ValueError("production-control journal builder row count changed during build")
    readiness_pull = _drum_readiness_pull_by_run_item(db, int(generation.id))
    proposal_rows = list_make_proposals(
        db,
        ledger_generation_id=int(generation.id),
        accepted_run_ids=run_ids,
        readiness_pull_by_run_item=readiness_pull,
    )
    from app.services.production_control_material_availability import (
        preview_make_work_item_materials,
        preview_make_work_items_coverage,
    )
    proposal_coverage = preview_make_work_items_coverage(
        db,
        proposal_rows,
        ledger_generation_id=int(generation.id),
    )
    for row in proposal_rows:
        coverage = proposal_coverage.get(int(row["work_item_id"]))
        if coverage is not None:
            row["coverage_status"] = coverage["coverage_status"]
            row["coverage_label"] = coverage["coverage_label"]
            row["material_coverage_status"] = coverage["coverage_status"]
            row["material_coverage_label"] = coverage["coverage_label"]
            row["material_coverage_calculated_at"] = generation.cutoff.isoformat()
        # Persist the exact proposal quantity coverage at publication time.
        # Current GETs must not replay BOM/ledger/custody; a different requested
        # quantity is therefore rejected until a worker publishes that quantity.
        if row.get("spec_id") is not None and row.get("launchable_qty") not in (None, 0):
            row["material_coverage_snapshot"] = preview_make_work_item_materials(
                db,
                work_item_id=int(row["work_item_id"]),
                item_id=int(row["item_id"]),
                quantity=float(row["launchable_qty"]),
                spec_id=int(row["spec_id"]),
                ledger_generation_id=int(generation.id),
                order_number=f"MRP-R-{int(row['source_mrp_requirement_id'])}",
                run_id=int(row["source_run_id"]) if row.get("source_run_id") is not None else None,
            )
        source_run_id = row.get("source_run_id")
        pull = (
            readiness_pull.get((int(source_run_id), int(row["item_id"])))
            if source_run_id is not None else None
        )
        if pull is not None:
            row.update(deepcopy(pull))
            row["launch_source"] = "drum_readiness"
    rows.extend(proposal_rows)
    product_ids = [
        int(row["product_id"])
        for row in rows
        if row.get("product_id") is not None
    ]
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("production-control journal candidate has duplicate products")

    route_snapshots = build_route_sheet_snapshot_payloads(
        db,
        product_ids=product_ids,
        ledger_generation_id=int(generation.id),
    )
    for row in rows:
        if row.get("product_id") is None:
            continue
        product_id = int(row["product_id"])
        row_snapshot = route_snapshots.get(product_id)
        if not isinstance(row_snapshot, Mapping):
            raise ValueError("production-control journal candidate is missing route-sheet snapshot")
        row["_route_sheet_snapshot"] = deepcopy(row_snapshot)
    return rows, {
        "latest_run_id": latest_run_id,
        "latest_source_plan_id": latest_source_plan_id,
    }


def _root_membership_by_row(
    db: Session,
    *,
    rows: Sequence[Mapping[str, Any]],
    accepted_run_ids: Sequence[int],
) -> dict[str, set[int]]:
    run_ids = sorted({int(value) for value in accepted_run_ids})
    if not rows or not run_ids:
        return {}
    run_plan_rows = (
        db.query(models.PlanningRun.run_id, models.PlanningRun.source_plan_id)
        .filter(models.PlanningRun.run_id.in_(run_ids))
        .filter(models.PlanningRun.source_plan_id.isnot(None))
        .all()
    )
    plan_by_run = {int(run_id): int(plan_id) for run_id, plan_id in run_plan_rows}
    if not plan_by_run:
        return {}
    roots_by_plan: dict[int, set[int]] = {}
    for plan_id, item_id in (
        db.query(models.ProductionPlanLine.plan_id, models.ProductionPlanLine.item_id)
        .filter(models.ProductionPlanLine.plan_id.in_(sorted(set(plan_by_run.values()))))
        .all()
    ):
        roots_by_plan.setdefault(int(plan_id), set()).add(int(item_id))
    root_ids = sorted({root for roots in roots_by_plan.values() for root in roots})
    descendants = (
        BomSpecificationResolver(db).descendant_ids_by_root(root_ids)
        if root_ids
        else {}
    )
    result: dict[str, set[int]] = {}
    for row in rows:
        source_run_id = row.get("source_run_id")
        if source_run_id is None:
            continue
        plan_id = plan_by_run.get(int(source_run_id))
        if plan_id is None:
            continue
        item_id = int(row["item_id"])
        matched = {
            root_id
            for root_id in roots_by_plan.get(plan_id, set())
            if item_id in descendants.get(root_id, {root_id})
        }
        if matched:
            result[str(row["journal_row_key"])] = matched
    return result




def _candidate_business_identity(payload: Mapping[str, Any]) -> str:
    requirement_id = payload.get("source_mrp_requirement_id") or payload.get("requirement_id")
    if requirement_id not in (None, ""):
        discriminator = (
            payload.get("source_mrp_allocation_key")
            or payload.get("source_mrp_allocation_id")
            or payload.get("item_id")
            or "default"
        )
        return f"production-mrp-requirement:{int(requirement_id)}:{discriminator}"
    order_id = payload.get("order_id")
    if order_id not in (None, ""):
        line = payload.get("line_number") or payload.get("product_id") or payload.get("item_id")
        if line in (None, ""):
            raise ValueError("production order row lacks stable line identity")
        return f"production-order-line:{int(order_id)}:{line}"
    journal_key = str(payload.get("journal_row_key") or payload.get("row_key") or "")
    if journal_key.startswith("work-item:"):
        raise ValueError("production proposal lacks stable MRP requirement identity")
    if not journal_key:
        raise ValueError("production row lacks stable identity")
    return journal_key


def _build_candidate_components(
    db: Session,
    generation_id: int,
    *,
    accepted_run_ids: Sequence[int],
) -> tuple[models.LedgerGeneration, dict[str, Any], list[dict[str, Any]], dict[str, set[int]]]:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if (
        generation is None
        or str(generation.status) != "building"
        or generation.cutoff is None
    ):
        raise ValueError(
            "production-control journal candidate requires BUILDING Ledger generation"
        )
    verify_future_supply_capture(
        db,
        int(generation.id),
    )
    run_ids = tuple(sorted({int(value) for value in accepted_run_ids}))
    rows, journal_meta = _build_rows(db, generation, run_ids)
    roots_by_row = _root_membership_by_row(
        db,
        rows=rows,
        accepted_run_ids=run_ids,
    )
    root_product_options = _root_product_options(db, roots_by_row)
    payload = {
        "meta": {
            "ledger_generation_id": int(generation.id),
            "cutoff": generation.cutoff.isoformat(),
            "truth_status": "building",
            "read_only": True,
            "row_count": len(rows),
            "accepted_run_ids": list(run_ids),
            "root_product_options": root_product_options,
            **journal_meta,
        }
    }
    return generation, payload, rows, roots_by_row


def build_candidate_payload(
    db: Session,
    generation_id: int,
    *,
    accepted_run_ids: Sequence[int],
) -> dict[str, Any]:
    """Build the production journal candidate without persisting snapshot rows.

    The returned structure is the direct input to the compact current owner.
    Each row carries its exact root membership so runtime publication does not
    need an
    intermediate owner.
    """
    _generation, payload, rows, roots_by_row = _build_candidate_components(
        db,
        generation_id,
        accepted_run_ids=accepted_run_ids,
    )
    direct_rows: list[dict[str, Any]] = []
    for row in rows:
        direct = dict(row)
        direct["current_identity"] = _candidate_business_identity(direct)
        direct["root_item_ids"] = sorted(
            int(value) for value in roots_by_row.get(str(row["journal_row_key"]), set())
        )
        direct_rows.append(direct)
    return {
        **payload,
        "rows": direct_rows,
        "summary": {"total_rows": len(direct_rows)},
    }




def validate_candidate_payload(
    payload: Mapping[str, Any],
    generation: models.LedgerGeneration,
) -> None:
    """Validate a direct production candidate without querying snapshot rows."""
    meta = payload.get("meta") if isinstance(payload, Mapping) else None
    if (
        not isinstance(meta, Mapping)
        or meta.get("read_only") is not True
        or int(meta.get("ledger_generation_id") or -1) != int(generation.id)
        or meta.get("truth_status") not in {"building", "accepted"}
    ):
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate is missing or stale"
        )
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate rows are missing"
        )
    try:
        expected_count = int(meta.get("row_count", len(rows)))
    except (TypeError, ValueError) as exc:
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate row count is malformed"
        ) from exc
    if expected_count < 0 or expected_count != len(rows):
        raise ProductionControlJournalPromotionError(
            "production-control journal candidate rows are incomplete"
        )
    product_ids: set[int] = set()
    work_item_ids: set[int] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate row is malformed"
            )
        row = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else raw
        try:
            item_id = int(row["item_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate row is malformed"
            ) from exc
        if item_id <= 0:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate row is malformed"
            )
        root_ids = row.get("root_item_ids", [])
        if not isinstance(root_ids, (list, tuple)):
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate root membership is malformed"
            )
        try:
            if any(int(root_id) <= 0 for root_id in root_ids):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate root membership is malformed"
            ) from exc
        if row.get("product_id") is None:
            try:
                work_item_id = int(row["work_item_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProductionControlJournalPromotionError(
                    "production-control journal proposal row is malformed"
                ) from exc
            if work_item_id <= 0 or work_item_id in work_item_ids:
                raise ProductionControlJournalPromotionError(
                    "production-control journal proposal row is malformed"
                )
            work_item_ids.add(work_item_id)
            continue
        try:
            product_id = int(row["product_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate row is malformed"
            ) from exc
        if product_id <= 0 or product_id in product_ids:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate row is malformed"
            )
        if "_route_sheet_snapshot" not in row:
            raise ProductionControlJournalPromotionError(
                "production-control journal candidate row is missing route-sheet snapshot"
            )
        _route_sheet_payload_value(row, product_id=product_id)
        product_ids.add(product_id)






def read_route_sheet_snapshot_rows(
    db: Session,
    product_ids: Sequence[int],
) -> list[dict[str, Any]]:
    ids = [int(product_id) for product_id in product_ids if product_id is not None]
    if not ids:
        return []

    from app.services.item_ledger.current_execution import (
        CurrentExecutionUnavailable,
        load_current_execution_rows,
        require_current_execution_scope,
    )
    try:
        manifest = require_current_execution_scope(
            db,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )
    except CurrentExecutionUnavailable as exc:
        raise _route_sheet_unavailable(db, str(exc)) from exc

    product_ids_sorted = sorted(set(ids))
    rows = [
        row for row in load_current_execution_rows(
            db,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )
        if int((row.payload or {}).get("product_id") or -1) in product_ids_sorted
    ]

    route_snapshot_by_product_id: dict[int, dict[str, Any]] = {}
    for row in rows:
        row_payload = row.payload if isinstance(row.payload, dict) else None
        if not isinstance(row_payload, dict):
            raise _route_sheet_unavailable(
                db,
                "accepted production-control route-sheets snapshot rows are malformed",
            )
        try:
            route_product_id = int(row_payload["product_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _route_sheet_unavailable(
                db,
                "accepted production-control route-sheets snapshot rows are malformed",
            ) from exc
        if "_route_sheet_snapshot" not in row_payload:
            raise _route_sheet_unavailable(
                db,
                "accepted production-control route-sheets snapshot rows are missing payload",
            )
        try:
            route_snapshot = _route_sheet_payload_value(
                row_payload,
                product_id=route_product_id,
            )
        except ProductionControlJournalPromotionError as exc:
            raise _route_sheet_unavailable(
                db,
                "accepted production-control route-sheets snapshot rows are malformed",
            ) from exc
        route_snapshot_by_product_id[route_product_id] = route_snapshot

    missing_ids = sorted(set(ids) - set(route_snapshot_by_product_id))
    if missing_ids:
        # Aggregated paint-weld journal rows persist one route-sheet payload on
        # the painted anchor. Resolve a requested welded product from that
        # same immutable snapshot instead of requiring a duplicate row.
        missing_set = set(missing_ids)
        anchor_rows = rows
        for row in anchor_rows:
            row_payload = row.payload if isinstance(row.payload, dict) else None
            if not isinstance(row_payload, dict) or "_route_sheet_snapshot" not in row_payload:
                continue
            try:
                anchor_product_id = int(row_payload["product_id"])
                route_snapshot = _route_sheet_payload_value(
                    row_payload,
                    product_id=anchor_product_id,
                )
                chain = route_snapshot.get("sheet", {}).get("chain") or {}
                weld_product_id = int(chain.get("weld_product_id"))
            except (KeyError, TypeError, ValueError, ProductionControlJournalPromotionError):
                continue
            if weld_product_id in missing_set:
                route_snapshot_by_product_id[weld_product_id] = route_snapshot
                missing_set.remove(weld_product_id)
                if not missing_set:
                    break
        missing_ids = sorted(missing_set)
    if missing_ids:
        # Изделие, созданное после cutoff принятого поколения, в снимок попасть
        # не могло.  Маршрутный лист — документ по физическому заказу, а не
        # плановая гипотеза, поэтому он печатается сразу после запуска.
        live_payloads = route_sheets_after_cutoff(
            db,
            missing_ids,
            cutoff=_generation_cutoff(db, manifest.source_generation_id),
            ledger_generation_id=int(manifest.source_generation_id),
        )
        for product_id, payload in live_payloads.items():
            route_snapshot_by_product_id[int(product_id)] = dict(payload)
        missing_ids = sorted(set(missing_ids) - set(live_payloads))
    if missing_ids:
        raise _route_sheet_unavailable(
            db,
            "accepted production-control route-sheets snapshot does not contain "
            + ", ".join(str(product_id) for product_id in missing_ids),
        )

    ordered: list[dict[str, Any]] = []
    seen_anchors: set[int] = set()
    for product_id in ids:
        route_snapshot = route_snapshot_by_product_id.get(product_id)
        if route_snapshot is None:
            continue
        try:
            anchor_product_id = int(route_snapshot.get("anchor_product_id"))
        except (TypeError, ValueError) as exc:
            raise _route_sheet_unavailable(
                db,
                "accepted production-control route-sheets snapshot rows are malformed",
            ) from exc
        if anchor_product_id in seen_anchors:
            continue
        seen_anchors.add(anchor_product_id)
        ordered.append(route_snapshot)
    return ordered




def read_snapshot(
    db: Session,
    *,
    product_id: int | None = None,
    order_id: int | None = None,
    root_item_id: int | None = None,
    workshop_id: int | None = None,
    status: str | None = None,
    coverage_status: str | None = None,
    planning_contour: str | None = None,
    launch_source: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Read the accepted production journal from compact current rows only."""
    from app.services.item_ledger.current_execution import (
        CurrentExecutionUnavailable,
        load_current_execution_rows,
        require_current_execution_scope,
    )
    try:
        manifest = require_current_execution_scope(
            db,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )
        records = load_current_execution_rows(
            db,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )
    except CurrentExecutionUnavailable as exc:
        raise _unavailable(db, str(exc)) from exc

    def _matches(row: Any) -> bool:
        payload = dict(row.payload or {})
        if product_id is not None and int(payload.get("product_id") or -1) != int(product_id):
            return False
        if order_id is not None and int(payload.get("order_id") or -1) != int(order_id):
            return False
        roots = {int(value) for value in payload.get("root_item_ids") or []}
        if root_item_id is not None and int(root_item_id) not in roots:
            return False
        if workshop_id is not None and int(payload.get("workshop_id") or -1) != int(workshop_id):
            return False
        if status:
            values = STATUS_FILTER_GROUPS.get(str(status), (str(status),))
            if str(payload.get("status") or "") not in values:
                return False
        if coverage_status and str(payload.get("coverage_status") or "") != str(coverage_status):
            return False
        if planning_contour:
            contour = str(planning_contour).strip().lower()
            if contour not in {"mrp", "1c"}:
                raise ValueError("unknown planning_contour")
            if str(payload.get("order_source") or "").lower() != contour:
                return False
        if launch_source and str(payload.get("launch_source") or "") != str(launch_source).strip():
            return False
        if search and search.strip():
            needle = search.strip().lower()
            if not any(needle in str(payload.get(key) or "").lower() for key in (
                "order_number", "item_name", "item_article", "item_code",
            )):
                return False
        row_date = str(payload.get("order_date") or "")
        if date_from and row_date < str(date_from):
            return False
        if date_to and row_date > str(date_to):
            return False
        return True

    completed_order_ids = {
        int(order_id)
        for order_id in db.execute(
            select(models.ProductionOrder.order_id).where(
                func.lower(func.coalesce(models.ProductionOrder.order_state_key, ""))
                == DONE_STATE_KEY
            )
        ).scalars().all()
    }
    filtered = [
        row
        for row in records
        if _matches(row)
        and int((dict(row.payload or {}).get("order_id") or 0)) not in completed_order_ids
    ]
    sort_key = str(sort_by or "").strip().lower()
    sortable_primary_fields = {
        "planned_start_date", "planned_finish_date", "readiness_need_date",
        "readiness_action_date", "readiness_priority_key",
    }
    if sort_key in sortable_primary_fields:
        primary_field = sort_key
        descending = str(sort_dir or "").strip().lower() == "desc"
    else:
        primary_field = "order_date"
        descending = True

    def _tie_key(row: Any) -> tuple[str, int, str]:
        payload = dict(row.payload or {})
        raw_line = payload.get("line_number")
        try:
            line_number = int(raw_line) if raw_line is not None else 0
        except (TypeError, ValueError):
            line_number = 0
        return (
            str(payload.get("order_number") or ""),
            line_number,
            str(raw_line or ""),
        )

    # Keep business tie-breakers ascending regardless of the primary
    # direction. Partitioning also makes NULLS LAST explicit instead of
    # allowing ``reverse=True`` to move missing values to the front.
    filtered.sort(key=_tie_key)
    present = [
        row for row in filtered
        if dict(row.payload or {}).get(primary_field) is not None
    ]
    missing = [
        row for row in filtered
        if dict(row.payload or {}).get(primary_field) is None
    ]
    present.sort(
        key=lambda row: str(dict(row.payload or {}).get(primary_field)),
        reverse=descending,
    )
    filtered[:] = [*present, *missing]
    total = len(filtered)
    effective_limit = max(1, min(int(limit or 100), 500))
    requested_offset = max(0, int(offset or 0))
    max_offset = max(0, ((total - 1) // effective_limit) * effective_limit) if total else 0
    effective_offset = min(requested_offset, max_offset)
    page = filtered[effective_offset:effective_offset + effective_limit]
    public_rows = []
    for row in page:
        public = _public_journal_row(dict(row.payload or {}))
        public["current_identity"] = str(row.business_identity)
        public["source_revision"] = str(manifest.source_revision)
        public_rows.append(public)
    # Execution overlays are mutable operational facts, not a second planning
    # snapshot.  Apply them to the persisted current projection while keeping
    # the accepted planning quantities and coverage unchanged.
    overlay_launch_facts(
        db,
        public_rows,
        cutoff=_generation_cutoff(db, manifest.source_generation_id),
    )
    overlay_execution_state(db, public_rows)
    summary = dict(manifest.summary or {})
    return {
        "rows": public_rows,
        "total": total,
        "limit": effective_limit,
        "offset": effective_offset,
        "latest_run_id": summary.get("latest_run_id"),
        "latest_source_plan_id": summary.get("latest_source_plan_id"),
        "source_revision": str(manifest.source_revision),
    }
