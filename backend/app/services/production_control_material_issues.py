from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload

from ..models import (
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    StockWarehouse,
    SyncLink,
    WorkshopWarehouseBinding,
)
from ..schemas import ODataSyncRequest
from .production_control_common import date_to_iso as _date_to_iso, to_float as _to_float
from .production_control_domain import ensure_state as _ensure_state, unit_display as _unit_display
from .production_control_material_availability import _components_for_product
from .one_c_export_common import (
    clean_ref1c as _clean_ref1c,
    create_odata_client as _create_odata_client,
    post_document_operational as _post_document_operational,
)
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient
from .one_c_stock_transfer_export import STOCK_TRANSFER_ENTITY
from .one_c_document_numbers import material_issue_number
from .production_control_reservations import (
    ReservationState,
    TRANSIT_STATUSES,
    is_product_reservation_active,
    load_reservation_state,
)
from .workshop_resolution import (
    diagnose_product,
    format_diagnosis_error,
    resolve_workshop_for_product,
    warehouse_binding_for_workshop,
)

PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"
DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"
HIDDEN_ORDER_LINE_STATUSES = {"produced", "done", "completed", "cancelled"}


def _clean_odata_error_message(error: Exception) -> str:
    raw = str(error or "")
    match = re.search(r"Details:\s*(\{.*\})", raw, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(1))
            value = (
                data.get("odata.error", {})
                .get("message", {})
                .get("value")
            )
            if value:
                return str(value)
        except Exception:
            pass
    if "URL:" in raw:
        raw = raw.split("URL:", 1)[0].strip()
    return raw.strip("<> ") or "1С отказала в проведении перемещения"


def _auto_select_source_warehouse(
    db: Session,
    component_item_ids: List[int],
    *,
    excluded_refs: Optional[set[str]] = None,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Given a list of component item_ids, find the best source warehouse.

    Returns (selected_ref1c, candidates) where:
    - selected_ref1c is the auto-chosen warehouse ref (or None if ambiguous/none)
    - candidates is a list of dicts with {ref1c, name, components_covered, total_components}

    Excludes ignored_warehouses. Ignores warehouses with is_selected=False
    (those are deliberately excluded from stock accounting).

    A warehouse is "best" if it covers the most distinct components with qty>0.
    If exactly one warehouse covers the maximum — it is auto-selected.
    If there's a tie — returns None + all tied candidates so UI can ask.
    """
    if not component_item_ids:
        return None, []

    total = len(component_item_ids)

    ignored_refs = {
        str(r[0])
        for r in db.query(IgnoredWarehouse.warehouse_ref1c).all()
    }
    excluded_refs = {str(ref) for ref in (excluded_refs or set()) if str(ref)}
    selected_refs = {
        str(r[0])
        for r in db.query(StockWarehouse.warehouse_ref1c)
        .filter(StockWarehouse.is_selected.is_(True))
        .all()
    }

    rows = (
        db.query(ItemWarehouseStock.warehouse_ref1c, ItemWarehouseStock.item_id)
        .filter(
            ItemWarehouseStock.item_id.in_(component_item_ids),
            ItemWarehouseStock.qty > 0,
        )
        .all()
    )

    coverage: Dict[str, set] = {}
    for wh_ref, item_id in rows:
        wh_ref = str(wh_ref)
        if wh_ref in ignored_refs:
            continue
        if wh_ref in excluded_refs:
            continue
        if selected_refs and wh_ref not in selected_refs:
            continue
        coverage.setdefault(wh_ref, set()).add(int(item_id))

    if not coverage:
        return None, []

    wh_names: Dict[str, str] = {
        str(r[0]): str(r[1] or r[0])
        for r in db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.warehouse_name).all()
    }

    max_covered = max(len(v) for v in coverage.values())
    candidates = [
        {
            "ref1c": ref,
            "name": wh_names.get(ref, ref),
            "components_covered": len(covered),
            "total_components": total,
        }
        for ref, covered in coverage.items()
        if len(covered) == max_covered
    ]

    if len(candidates) == 1:
        return candidates[0]["ref1c"], candidates

    return None, candidates


def _source_warehouse_options(
    db: Session,
    component_item_ids: List[int],
    *,
    excluded_refs: Optional[set[str]] = None,
) -> Dict[int, List[Dict[str, Any]]]:
    if not component_item_ids:
        return {}

    ignored_refs = {
        str(r[0])
        for r in db.query(IgnoredWarehouse.warehouse_ref1c).all()
    }
    excluded_refs = {str(ref) for ref in (excluded_refs or set()) if str(ref)}
    selected_refs = {
        str(r[0])
        for r in db.query(StockWarehouse.warehouse_ref1c)
        .filter(StockWarehouse.is_selected.is_(True))
        .all()
    }
    wh_names: Dict[str, str] = {
        str(r[0]): str(r[1] or r[0])
        for r in db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.warehouse_name).all()
    }

    rows = (
        db.query(
            ItemWarehouseStock.item_id,
            ItemWarehouseStock.warehouse_ref1c,
            func.sum(ItemWarehouseStock.qty),
        )
        .filter(
            ItemWarehouseStock.item_id.in_(component_item_ids),
            ItemWarehouseStock.qty > 0,
        )
        .group_by(ItemWarehouseStock.item_id, ItemWarehouseStock.warehouse_ref1c)
        .all()
    )

    result: Dict[int, List[Dict[str, Any]]] = {}
    for item_id, wh_ref, qty in rows:
        ref = str(wh_ref)
        if ref in ignored_refs or ref in excluded_refs:
            continue
        if selected_refs and ref not in selected_refs:
            continue
        result.setdefault(int(item_id), []).append(
            {
                "ref1c": ref,
                "name": wh_names.get(ref, ref),
                "qty": _to_float(qty),
            }
        )
    for options in result.values():
        options.sort(key=lambda row: (-float(row.get("qty") or 0.0), str(row.get("name") or "")))
    return result


def _allocate_components_by_source_warehouse(
    db: Session,
    components: List[Dict[str, Any]],
    *,
    destination_warehouse_ref1c: Optional[str] = None,
    selected_source_warehouse_ref1c: Optional[str] = None,
) -> Tuple[Dict[Optional[str], List[Dict[str, Any]]], List[Dict[str, Any]]]:
    component_item_ids = [int(c["component_item_id"]) for c in components]
    excluded = {str(destination_warehouse_ref1c)} if destination_warehouse_ref1c else set()
    options_by_item = _source_warehouse_options(
        db,
        component_item_ids,
        excluded_refs=excluded,
    )
    selected_ref = _clean_ref1c(selected_source_warehouse_ref1c) or None

    groups: Dict[Optional[str], List[Dict[str, Any]]] = {}
    selection_required: List[Dict[str, Any]] = []
    for comp in components:
        component_id = int(comp["component_item_id"])
        options = options_by_item.get(component_id, [])
        if not options:
            groups.setdefault(None, []).append(comp)
            continue
        if len(options) == 1:
            groups.setdefault(str(options[0]["ref1c"]), []).append(comp)
            continue
        option_refs = {str(row["ref1c"]) for row in options}
        if not selected_ref or selected_ref not in option_refs:
            selection_required.append(
                {
                    "component_item_id": component_id,
                    "item_name": str(comp.get("item_name") or ""),
                    "item_article": str(comp.get("item_article") or ""),
                    "required_qty": _to_float(comp.get("required_qty")),
                    "warehouse_candidates": [
                        {
                            "ref1c": str(row["ref1c"]),
                            "name": str(row["name"]),
                            "qty": _to_float(row.get("qty")),
                        }
                        for row in options
                    ],
                }
            )
            continue
        groups.setdefault(selected_ref, []).append(comp)

    if selection_required:
        return {}, selection_required
    return groups, []


def _destination_stock_by_component(
    db: Session,
    components: List[Dict[str, Any]],
    destination_warehouse_ref1c: Optional[str],
) -> Dict[int, float]:
    destination_ref = _clean_ref1c(destination_warehouse_ref1c)
    if not destination_ref or not components:
        return {}
    component_ids = sorted({int(comp["component_item_id"]) for comp in components})
    rows = (
        db.query(ItemWarehouseStock.item_id, func.sum(ItemWarehouseStock.qty))
        .filter(
            ItemWarehouseStock.item_id.in_(component_ids),
            ItemWarehouseStock.warehouse_ref1c == destination_ref,
            ItemWarehouseStock.qty > 0,
        )
        .group_by(ItemWarehouseStock.item_id)
        .all()
    )
    return {int(item_id): _to_float(qty) for item_id, qty in rows}


def _components_still_to_move(
    db: Session,
    components: List[Dict[str, Any]],
    destination_warehouse_ref1c: Optional[str],
    consumed_destination_stock: Optional[Dict[Tuple[str, int], float]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Do not create a transfer for quantities that are already on the recipient
    workshop warehouse. If the workshop has a partial balance, move only the
    missing remainder.
    """
    destination_stock = _destination_stock_by_component(db, components, destination_warehouse_ref1c)
    if not destination_stock:
        return components, []

    destination_ref = _clean_ref1c(destination_warehouse_ref1c)
    consumed_destination_stock = consumed_destination_stock if consumed_destination_stock is not None else {}
    to_move: List[Dict[str, Any]] = []
    already_on_destination: List[Dict[str, Any]] = []
    for comp in components:
        component_id = int(comp["component_item_id"])
        required_qty = _to_float(comp.get("required_qty"))
        consumed_key = (destination_ref, component_id)
        available_qty = max(destination_stock.get(component_id, 0.0) - consumed_destination_stock.get(consumed_key, 0.0), 0.0)
        covered_qty = min(required_qty, available_qty)
        if covered_qty > 1e-9:
            consumed_destination_stock[consumed_key] = consumed_destination_stock.get(consumed_key, 0.0) + covered_qty
            already_on_destination.append(
                {
                    "component_item_id": component_id,
                    "item_name": str(comp.get("item_name") or ""),
                    "item_article": str(comp.get("item_article") or ""),
                    "required_qty": required_qty,
                    "covered_qty": covered_qty,
                    "remaining_qty": max(required_qty - covered_qty, 0.0),
                    "warehouse_ref1c": _clean_ref1c(destination_warehouse_ref1c),
                }
            )
        if available_qty + 1e-9 >= required_qty:
            continue
        next_comp = dict(comp)
        next_comp["required_qty"] = max(required_qty - available_qty, 0.0)
        if _to_float(next_comp.get("required_qty")) > 1e-9:
            to_move.append(next_comp)

    return to_move, already_on_destination


def _next_issue_number(db: Session) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"MI-{today}-"
    count = db.query(ProductionMaterialIssue).filter(ProductionMaterialIssue.document_number.like(f"{prefix}%")).count()
    return f"{prefix}{count + 1:04d}"


def _issue_reuse_payload(
    issue: ProductionMaterialIssue,
    product: ProductionProduct,
) -> Dict[str, Any]:
    return {
        "issue_id": int(issue.issue_id),
        "document_number": str(issue.document_number),
        "product_id": int(product.product_id),
        "order_number": str(product.order.order_number or ""),
        "item_name": str(product.item.item_name or ""),
        "status": str(issue.status),
        "source_warehouse_ref1c": str(issue.source_warehouse_ref1c or ""),
        "direction": str(issue.direction or "issue"),
    }


def _prodplan_order_display_number(product: Optional[ProductionProduct], order: Optional[ProductionOrder]) -> str:
    if order is None:
        return ""
    order_source = str(order.source or "1c")
    if order_source != "mrp" or product is None:
        return str(order.order_number or "")

    run_id = int(order.source_run_id) if order.source_run_id is not None else None
    planned_order_id = (
        int(product.source_planned_order_id)
        if getattr(product, "source_planned_order_id", None) is not None
        else None
    )
    if run_id is not None and planned_order_id is not None:
        return f"MRP-{run_id}-{planned_order_id}"

    requirement_id = (
        int(product.source_mrp_requirement_id)
        if getattr(product, "source_mrp_requirement_id", None) is not None
        else None
    )
    allocation_key = str(product.source_mrp_allocation_key or "")
    if requirement_id is not None and allocation_key.startswith(f"mrp_requirement:{requirement_id}:order:"):
        try:
            seq = int(allocation_key.rsplit(":", 1)[-1])
        except Exception:
            seq = 1
        return f"MRP-R-{requirement_id}" if seq <= 1 else f"MRP-R-{requirement_id}-{seq}"

    if run_id is not None and getattr(product, "item_id", None) is not None:
        return f"MRP-RC-{run_id}-{int(product.item_id)}-{int(order.order_id)}"

    return str(order.order_number or "")


def _order_one_c_number(db: Session, order: Optional[ProductionOrder]) -> str:
    if order is None:
        return ""
    link = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "production_order",
            SyncLink.source_id == int(order.order_id),
            SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
        )
        .one_or_none()
    )
    if link and link.target_number:
        return str(link.target_number)
    if order.order_ref1c and str(order.source or "1c") == "1c":
        return str(order.order_number or "")
    return ""


def _material_issue_sync_link(db: Session, issue_id: int) -> Optional[SyncLink]:
    return (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.source_id == int(issue_id),
            SyncLink.target_entity == STOCK_TRANSFER_ENTITY,
        )
        .one_or_none()
    )


def _material_issue_has_1c_link(db: Session, issue: ProductionMaterialIssue) -> bool:
    return bool(_clean_ref1c(issue.exported_ref1c) or _material_issue_sync_link(db, int(issue.issue_id)))


def refresh_existing_material_issues_for_product(
    db: Session,
    product: ProductionProduct,
) -> Dict[str, Any]:
    """
    Keep already-open local material issue quantities aligned after a line edit.

    Posted 1C documents are final here; they are reported back to the caller so
    the UI can tell the operator that a separate correction is needed.
    """
    existing_rows = (
        db.query(ProductionMaterialIssue)
        .options(joinedload(ProductionMaterialIssue.lines))
        .filter(
            ProductionMaterialIssue.product_id == int(product.product_id),
            ProductionMaterialIssue.direction == "issue",
            ProductionMaterialIssue.status.in_(("draft", "requested", "issued", "exported", "posted", "error")),
        )
        .order_by(ProductionMaterialIssue.issue_id.asc())
        .all()
    )
    if not existing_rows:
        return {"updated": [], "blocked": []}

    spec_id, components = _components_for_product(db, product)
    by_component = {int(comp["component_item_id"]): comp for comp in components}
    updated: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []

    for issue in existing_rows:
        if str(issue.status or "") == "posted":
            blocked.append(
                {
                    "issue_id": int(issue.issue_id),
                    "document_number": str(issue.document_number or ""),
                    "status": str(issue.status or ""),
                    "reason": "posted",
                }
            )
            continue

        changed_lines = 0
        for line in issue.lines or []:
            comp = by_component.get(int(line.component_item_id))
            if comp is None:
                continue
            old_required = _to_float(line.required_qty)
            new_required = _to_float(comp.get("required_qty"))
            if abs(old_required - new_required) > 1e-9:
                line.required_qty = new_required
                changed_lines += 1
                if str(line.line_status or "") == "planned":
                    line.issued_qty = 0.0
            line.unit = comp.get("unit")
            line.source_spec_id = spec_id

        if changed_lines:
            updated.append(
                {
                    "issue_id": int(issue.issue_id),
                    "document_number": str(issue.document_number or ""),
                    "changed_lines": int(changed_lines),
                }
            )

    return {"updated": updated, "blocked": blocked}


def delete_local_material_issue(db: Session, issue_id: int) -> Dict[str, Any]:
    issue = (
        db.query(ProductionMaterialIssue)
        .options(joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.control_state))
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .one_or_none()
    )
    if issue is None:
        raise ValueError("Заявка на перемещение не найдена")
    if _material_issue_has_1c_link(db, issue):
        raise ValueError("Заявка уже открыта в 1С, локальное удаление запрещено")
    product_id = int(issue.product_id)
    db.delete(issue)
    state = issue.product.control_state if issue.product else None
    if state is not None:
        active_count = (
            db.query(ProductionMaterialIssue)
            .filter(
                ProductionMaterialIssue.product_id == product_id,
                ProductionMaterialIssue.direction == "issue",
                ProductionMaterialIssue.status.in_(("draft", "requested", "issued", "exported", "posted", "error")),
                ProductionMaterialIssue.issue_id != int(issue_id),
            )
            .count()
        )
        if active_count == 0:
            state.issue_status = "not_requested"
            if state.status == "to_move":
                state.status = "shortage"
    db.commit()
    return {"status": "ok", "issue_id": int(issue_id), "deleted": True}


def _destination_warehouse_for_product(
    db: Session,
    product: ProductionProduct,
    spec_id: Optional[int],
) -> Optional[str]:
    workshop_id_resolved = resolve_workshop_for_product(db, product, spec_id=spec_id)
    binding = warehouse_binding_for_workshop(db, workshop_id_resolved)
    return _clean_ref1c(binding.warehouse_ref1c) if binding else None


def _free_destination_stock(
    db: Session,
    component_item_ids: List[int],
    destination_warehouse_ref1c: Optional[str],
    reservation_state: ReservationState,
) -> Dict[int, float]:
    """
    Free (unreserved by ANY order) component stock lying on the destination
    workshop warehouse. Only counted when the warehouse participates in stock
    accounting (selected, not ignored) — mirrors the coverage rules.
    """
    dest = _clean_ref1c(destination_warehouse_ref1c)
    if not dest or not component_item_ids:
        return {}
    ignored = {
        str(r[0]) for r in db.query(IgnoredWarehouse.warehouse_ref1c).all()
    }
    if dest in ignored:
        return {}
    selected_rows = db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.is_selected).all()
    if selected_rows:
        selected = {str(ref) for ref, is_sel in selected_rows if ref and bool(is_sel)}
        if dest not in selected:
            return {}
    rows = (
        db.query(ItemWarehouseStock.item_id, func.sum(ItemWarehouseStock.qty))
        .filter(
            ItemWarehouseStock.item_id.in_(component_item_ids),
            ItemWarehouseStock.warehouse_ref1c == dest,
        )
        .group_by(ItemWarehouseStock.item_id)
        .all()
    )
    result: Dict[int, float] = {}
    for item_id, qty in rows:
        cid = int(item_id)
        reserved = reservation_state.reserved_at_warehouse(dest, cid)
        free = _to_float(qty) - reserved
        if free > 1e-9:
            result[cid] = free
    return result


def _claim_components_in_place(
    db: Session,
    product: ProductionProduct,
    components: List[Dict[str, Any]],
    *,
    spec_id: Optional[int],
    destination_warehouse_ref1c: Optional[str],
    initiated_by: Optional[str],
) -> Optional[ProductionMaterialIssue]:
    """
    Record that components already lying on the workshop warehouse are taken
    by this order. Creates/extends a direction='in_place' issue: a local-only
    reservation document, posted immediately, never exported to 1C (1C has no
    reservation concept — the components physically stay where they are and
    get written off the workshop by the closing СборкаЗапасов).
    """
    dest = _clean_ref1c(destination_warehouse_ref1c)
    issue = (
        db.query(ProductionMaterialIssue)
        .options(joinedload(ProductionMaterialIssue.lines))
        .filter(
            ProductionMaterialIssue.product_id == int(product.product_id),
            ProductionMaterialIssue.direction == "in_place",
            ProductionMaterialIssue.status == "posted",
        )
        .order_by(ProductionMaterialIssue.issue_id.desc())
        .first()
    )
    if issue is None:
        issue = ProductionMaterialIssue(
            document_number="",
            product_id=int(product.product_id),
            order_id=int(product.order_id),
            status="posted",
            direction="in_place",
            warehouse_ref1c=dest,
            source_warehouse_ref1c=dest,
            initiated_by=initiated_by,
        )
        db.add(issue)
        db.flush()
        issue.document_number = material_issue_number(db, issue)
    lines_by_component = {
        int(line.component_item_id): line for line in (issue.lines or [])
    }
    for comp in components:
        cid = int(comp["component_item_id"])
        qty = _to_float(comp["claim_qty"])
        if qty <= 1e-9:
            continue
        line = lines_by_component.get(cid)
        if line is None:
            db.add(
                ProductionMaterialIssueLine(
                    issue_id=int(issue.issue_id),
                    component_item_id=cid,
                    required_qty=qty,
                    issued_qty=qty,
                    unit=comp.get("unit"),
                    source_spec_id=spec_id,
                    line_status="issued",
                )
            )
        else:
            line.required_qty = _to_float(line.required_qty) + qty
            line.issued_qty = _to_float(line.issued_qty) + qty
    return issue


def _add_delta_to_issue(
    db: Session,
    issue: ProductionMaterialIssue,
    components: List[Dict[str, Any]],
    *,
    spec_id: Optional[int],
) -> None:
    """Grow a non-posted transfer by the outstanding delta (never shrinks)."""
    lines_by_component = {
        int(line.component_item_id): line for line in (issue.lines or [])
    }
    for comp in components:
        cid = int(comp["component_item_id"])
        delta = _to_float(comp["required_qty"])
        if delta <= 1e-9:
            continue
        line = lines_by_component.get(cid)
        if line is None:
            db.add(
                ProductionMaterialIssueLine(
                    issue_id=int(issue.issue_id),
                    component_item_id=cid,
                    required_qty=delta,
                    issued_qty=0.0,
                    unit=comp.get("unit"),
                    source_spec_id=spec_id,
                    line_status="planned",
                )
            )
        else:
            line.required_qty = _to_float(line.required_qty) + delta


def _shrink_transit_reservations(
    db: Session,
    issues: List[ProductionMaterialIssue],
    excess_by_component: Dict[int, float],
) -> None:
    """
    Release over-reservation after the order quantity went down. Non-posted
    transfer lines shrink (newest documents first); in_place claims release
    by decreasing both required and issued. Posted transfers stay — physical
    leftovers go back via the return flow.
    """
    def _release_from(issue: ProductionMaterialIssue, *, in_place: bool) -> None:
        for line in sorted(issue.lines or [], key=lambda l: l.line_id, reverse=True):
            cid = int(line.component_item_id)
            excess = excess_by_component.get(cid, 0.0)
            if excess <= 1e-9:
                continue
            if in_place:
                current = _to_float(line.issued_qty)
                take = min(current, excess)
                line.issued_qty = current - take
                line.required_qty = max(0.0, _to_float(line.required_qty) - take)
            else:
                current = max(0.0, _to_float(line.required_qty) - _to_float(line.issued_qty))
                take = min(current, excess)
                line.required_qty = _to_float(line.required_qty) - take
            excess_by_component[cid] = excess - take

    ordered = sorted(issues, key=lambda i: i.issue_id, reverse=True)
    for issue in ordered:
        if str(issue.direction or "") == "issue" and str(issue.status or "") in (
            "draft",
            "requested",
            "issued",
            "exported",
        ):
            _release_from(issue, in_place=False)
    for issue in ordered:
        if str(issue.direction or "") == "in_place" and str(issue.status or "") == "posted":
            _release_from(issue, in_place=True)


# Arbitrary stable key for the transaction-scoped advisory lock that serializes
# concurrent material-issue creation. Two parallel requests for different
# products that share a component on the same workshop warehouse would otherwise
# both read the same free stock and both claim it (section-stock double count,
# case PP001308915). The lock auto-releases when the transaction commits/rolls back.
_MATERIAL_ISSUE_LOCK_KEY = 0x70726D6973  # "prmis"


def _lock_material_issue_pool(db: Session) -> None:
    """Serialize the free-stock read-modify-write across concurrent callers.

    PostgreSQL only — a transaction advisory lock held until commit. On other
    backends (SQLite in tests) this is a no-op; real deployments run on Postgres.
    """
    bind = db.get_bind()
    if getattr(bind.dialect, "name", "") != "postgresql":
        return
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _MATERIAL_ISSUE_LOCK_KEY})


def create_material_issues(
    db: Session,
    product_ids: Sequence[int],
    *,
    initiated_by: Optional[str] = None,
    warehouse_ref1c: Optional[str] = None,
    source_warehouse_ref1c: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Bring the order's component reservations up to its BOM requirement.

    For every production line the outstanding need is computed as
    ``required - already reserved for this line`` (kits in transit + kits on
    the workshop). The outstanding part is covered in two steps:

    1. Free stock already lying on the destination workshop warehouse is
       claimed in place (direction='in_place', no 1C document) — the rule
       "компоненты на участке списываются с участка".
    2. Only the remainder becomes physical transfer requests, so storekeepers
       are never asked to move a full kit that partially exists at the
       destination already.

    Idempotent: a repeated click with nothing outstanding returns the
    existing documents in `reused`. After the order quantity shrinks, open
    (non-posted) reservations are released down to the requirement.
    """
    # Hold a transaction-scoped lock for the whole read-modify-write below so
    # concurrent callers cannot both claim the same free workshop stock.
    _lock_material_issue_pool(db)

    created: List[Dict[str, Any]] = []
    reused: List[Dict[str, Any]] = []
    selection_required: List[Dict[str, Any]] = []
    errors: List[str] = []
    already_on_destination: List[Dict[str, Any]] = []
    consumed_destination_stock: Dict[Tuple[str, int], float] = {}
    for pid in product_ids:
        product = (
            db.query(ProductionProduct)
            .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.item))
            .filter(ProductionProduct.product_id == int(pid))
            .first()
        )
        if not product:
            errors.append(f"product_id={pid}: строка заказа не найдена")
            continue
        if not is_product_reservation_active(product):
            errors.append(
                f"product_id={pid}: строка заказа уже закрыта или завершена в 1С; "
                "новые перемещения не создаются"
            )
            continue

        existing_rows = (
            db.query(ProductionMaterialIssue)
            .options(joinedload(ProductionMaterialIssue.lines))
            .filter(
                ProductionMaterialIssue.product_id == int(product.product_id),
                ProductionMaterialIssue.status.in_(("draft", "requested", "issued", "exported", "posted", "error")),
                ProductionMaterialIssue.direction.in_(("issue", "in_place")),
            )
            .order_by(ProductionMaterialIssue.issue_id.desc())
            .all()
        )

        spec_id, components = _components_for_product(db, product)
        if not components:
            if not spec_id:
                errors.append(
                    format_diagnosis_error(f"product_id={pid}", diagnose_product(db, product))
                )
            else:
                errors.append(f"product_id={pid}: в спецификации нет материалов")
            continue

        # If the caller did not pin a destination warehouse, fall back to the
        # workshop->warehouse binding from settings. Plan rule:
        # "привязка участок -> склад получатель".
        resolved_warehouse = warehouse_ref1c
        if not resolved_warehouse:
            resolved_warehouse = _destination_warehouse_for_product(db, product, spec_id)
        if not resolved_warehouse:
            # No explicit destination and the kind->workshop->warehouse chain
            # does not resolve: refuse instead of creating a transfer with an
            # empty destination. The diagnosis names the exact fix.
            errors.append(
                format_diagnosis_error(f"product_id={pid}", diagnose_product(db, product))
            )
            continue

        comp_ids = [int(c["component_item_id"]) for c in components]
        reservation_state = load_reservation_state(db, item_ids=comp_ids)
        own = reservation_state.for_product(int(product.product_id))

        outstanding: Dict[int, float] = {}
        excess: Dict[int, float] = {}
        for comp in components:
            cid = int(comp["component_item_id"])
            need = _to_float(comp["required_qty"]) - own.total(cid)
            if need > 1e-9:
                outstanding[cid] = need
            elif need < -1e-9:
                excess[cid] = -need

        if excess:
            _shrink_transit_reservations(db, existing_rows, excess)

        if not outstanding:
            for existing in existing_rows:
                if not existing.warehouse_ref1c:
                    existing.warehouse_ref1c = resolved_warehouse
                reused.append(_issue_reuse_payload(existing, product))
            continue

        outstanding_components = [
            {**comp, "required_qty": outstanding[int(comp["component_item_id"])]}
            for comp in components
            if int(comp["component_item_id"]) in outstanding
        ]

        # Step 1 (planned, applied after source selection succeeds): claim
        # free stock already on the destination workshop.
        free_dest = _free_destination_stock(
            db,
            [int(c["component_item_id"]) for c in outstanding_components],
            resolved_warehouse,
            reservation_state,
        )
        destination_ref = _clean_ref1c(resolved_warehouse)
        if destination_ref:
            for cid, qty in list(free_dest.items()):
                consumed_key = (destination_ref, int(cid))
                free_dest[int(cid)] = max(
                    _to_float(qty) - _to_float(consumed_destination_stock.get(consumed_key, 0.0)),
                    0.0,
                )
        claims: List[Dict[str, Any]] = []
        transfer_components: List[Dict[str, Any]] = []
        for comp in outstanding_components:
            cid = int(comp["component_item_id"])
            claim_qty = min(_to_float(comp["required_qty"]), free_dest.get(cid, 0.0))
            remainder = _to_float(comp["required_qty"]) - claim_qty
            if claim_qty > 1e-9:
                claims.append({**comp, "claim_qty": claim_qty})
                if destination_ref:
                    consumed_key = (destination_ref, cid)
                    consumed_destination_stock[consumed_key] = (
                        _to_float(consumed_destination_stock.get(consumed_key, 0.0)) + claim_qty
                    )
            if remainder > 1e-9:
                transfer_components.append({**comp, "required_qty": remainder})

        groups: Dict[Optional[str], List[Dict[str, Any]]] = {}
        if transfer_components:
            groups, needed_selection = _allocate_components_by_source_warehouse(
                db,
                transfer_components,
                destination_warehouse_ref1c=resolved_warehouse,
                selected_source_warehouse_ref1c=source_warehouse_ref1c,
            )
            if needed_selection:
                selection_required.append(
                    {
                        "product_id": int(product.product_id),
                        "order_number": str(product.order.order_number or ""),
                        "item_name": str(product.item.item_name or ""),
                        "components": needed_selection,
                        "warehouse_candidates": needed_selection[0]["warehouse_candidates"],
                    }
                )
                continue

        if claims:
            already_on_destination.append(
                {
                    "product_id": int(product.product_id),
                    "order_number": str(product.order.order_number or ""),
                    "item_name": str(product.item.item_name or ""),
                    "warehouse_ref1c": _clean_ref1c(resolved_warehouse),
                    "components": [
                        {
                            "component_item_id": int(comp["component_item_id"]),
                            "item_name": str(comp.get("item_name") or ""),
                            "item_article": str(comp.get("item_article") or ""),
                            "required_qty": _to_float(comp.get("required_qty")),
                            "covered_qty": _to_float(comp.get("claim_qty")),
                            "remaining_qty": max(
                                _to_float(comp.get("required_qty")) - _to_float(comp.get("claim_qty")),
                                0.0,
                            ),
                            "warehouse_ref1c": _clean_ref1c(resolved_warehouse),
                        }
                        for comp in claims
                    ],
                }
            )
            claim_issue = _claim_components_in_place(
                db,
                product,
                claims,
                spec_id=spec_id,
                destination_warehouse_ref1c=resolved_warehouse,
                initiated_by=initiated_by,
            )
            if claim_issue is not None:
                created.append(
                    {
                        "issue_id": int(claim_issue.issue_id),
                        "document_number": claim_issue.document_number,
                        "product_id": int(product.product_id),
                        "order_number": str(product.order.order_number or ""),
                        "item_name": str(product.item.item_name or ""),
                        "lines_count": len(claims),
                        "source_warehouse_ref1c": _clean_ref1c(resolved_warehouse),
                        "direction": "in_place",
                    }
                )

        existing_by_source = {
            str(row.source_warehouse_ref1c or ""): row
            for row in existing_rows
            if str(row.direction or "") == "issue"
            and str(row.status or "") in ("draft", "requested")
        }
        for resolved_source_wh, grouped_components in groups.items():
            source_key = str(resolved_source_wh or "")
            existing = existing_by_source.get(source_key)
            if existing is not None:
                if not existing.warehouse_ref1c:
                    existing.warehouse_ref1c = resolved_warehouse
                _add_delta_to_issue(db, existing, grouped_components, spec_id=spec_id)
                reused.append(_issue_reuse_payload(existing, product))
                continue

            issue = ProductionMaterialIssue(
                document_number="",
                product_id=int(product.product_id),
                order_id=int(product.order_id),
                status="draft",
                warehouse_ref1c=resolved_warehouse,
                source_warehouse_ref1c=resolved_source_wh,
                initiated_by=initiated_by,
            )
            db.add(issue)
            db.flush()
            issue.document_number = material_issue_number(db, issue)
            for comp in grouped_components:
                db.add(
                    ProductionMaterialIssueLine(
                        issue_id=int(issue.issue_id),
                        component_item_id=int(comp["component_item_id"]),
                        required_qty=float(comp["required_qty"]),
                        issued_qty=0.0,
                        unit=comp.get("unit"),
                        source_spec_id=spec_id,
                        line_status="planned",
                    )
                )
            entry: Dict[str, Any] = {
                "issue_id": int(issue.issue_id),
                "document_number": issue.document_number,
                "product_id": int(product.product_id),
                "order_number": str(product.order.order_number or ""),
                "item_name": str(product.item.item_name or ""),
                "lines_count": len(grouped_components),
                "source_warehouse_ref1c": resolved_source_wh,
            }
            created.append(entry)

        db.flush()
        has_open_transfers = any(
            str(row.direction or "") == "issue" and str(row.status or "") in TRANSIT_STATUSES
            for row in (
                db.query(ProductionMaterialIssue)
                .filter(ProductionMaterialIssue.product_id == int(product.product_id))
                .all()
            )
        )
        state = _ensure_state(db, product)
        if has_open_transfers or groups:
            state.issue_status = "requested"
            # Once material-issue drafts are open, the line has moved beyond
            # the "no coverage yet" phase. Bump status to 'to_move'
            # ("документы созданы, ждём проведения") unless it's already
            # further along.
            if state.status in {"shortage", "partial", "ready"}:
                state.status = "to_move"
        elif claims and not transfer_components:
            # Fully covered by components already on the workshop: nothing to
            # move, the line is assembled right away.
            state.issue_status = "posted"
            if state.status in {"shortage", "partial", "ready", "to_move"}:
                state.status = "assembled"
    db.commit()
    return {
        "status": "ok",
        "created": created,
        "reused": reused,
        "selection_required": selection_required,
        "already_on_destination": already_on_destination,
        "errors": errors,
    }


def _warehouse_name_lookup(db: Session, refs: Sequence[str]) -> Dict[str, str]:
    clean_refs = sorted({_clean_ref1c(ref) for ref in refs if _clean_ref1c(ref)})
    if not clean_refs:
        return {}
    return {
        str(row.warehouse_ref1c): str(row.warehouse_name or row.warehouse_ref1c)
        for row in db.query(StockWarehouse)
        .filter(StockWarehouse.warehouse_ref1c.in_(clean_refs))
        .all()
    }


def _warehouse_display_name(warehouse_names: Dict[str, str], ref: Optional[str]) -> str:
    clean_ref = _clean_ref1c(ref)
    if not clean_ref:
        return ""
    return warehouse_names.get(clean_ref, clean_ref)


def _issue_source_warehouse_options(db: Session, query) -> List[Dict[str, str]]:
    refs = [
        _clean_ref1c(row[0])
        for row in query.order_by(None)
        .with_entities(ProductionMaterialIssue.source_warehouse_ref1c)
        .filter(ProductionMaterialIssue.source_warehouse_ref1c.isnot(None))
        .distinct()
        .all()
    ]
    refs = sorted({ref for ref in refs if ref})
    warehouse_names = _warehouse_name_lookup(db, refs)
    return [
        {
            "warehouse_ref1c": ref,
            "warehouse_name": _warehouse_display_name(warehouse_names, ref),
        }
        for ref in sorted(refs, key=lambda value: _warehouse_display_name(warehouse_names, value).casefold())
    ]


def get_issue(db: Session, issue_id: int) -> Dict[str, Any]:
    issue = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.lines).joinedload(ProductionMaterialIssueLine.component_item),
        )
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .first()
    )
    if not issue:
        raise ValueError("Документ выдачи не найден")
    warehouse_names = _warehouse_name_lookup(
        db,
        [str(issue.source_warehouse_ref1c or ""), str(issue.warehouse_ref1c or "")],
    )
    link = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.source_id == int(issue.issue_id),
            SyncLink.target_entity == STOCK_TRANSFER_ENTITY,
        )
        .one_or_none()
    )
    one_c_number = str(link.target_number or "") if link else ""
    return {
        "issue_id": int(issue.issue_id),
        "document_number": str(issue.document_number),
        "status": str(issue.status),
        "warehouse_ref1c": str(issue.warehouse_ref1c or ""),
        "destination_warehouse_name": _warehouse_display_name(warehouse_names, issue.warehouse_ref1c),
        "source_warehouse_ref1c": str(issue.source_warehouse_ref1c or ""),
        "source_warehouse_name": _warehouse_display_name(warehouse_names, issue.source_warehouse_ref1c),
        "initiated_by": str(issue.initiated_by or ""),
        "order_number": str(issue.order.order_number or ""),
        "order_prodplan_number": _prodplan_order_display_number(issue.product, issue.order),
        "order_one_c_number": _order_one_c_number(db, issue.order),
        "product_id": int(issue.product_id),
        "item_name": str(issue.product.item.item_name or "") if issue.product and issue.product.item else "",
        "item_article": str(issue.product.item.item_article or "") if issue.product and issue.product.item else "",
        "created_at": _date_to_iso(issue.created_at),
        "exported_ref1c": str(issue.exported_ref1c or ""),
        "one_c_number": one_c_number,
        "export_error": str(issue.export_error or ""),
        "lines": [
            {
                "line_id": int(line.line_id),
                "component_item_id": int(line.component_item_id),
                "item_code": str(line.component_item.item_code or ""),
                "item_name": str(line.component_item.item_name or ""),
                "item_article": str(line.component_item.item_article or ""),
                "required_qty": _to_float(line.required_qty),
                "issued_qty": _to_float(line.issued_qty),
                "unit": _unit_display(db, line.unit or line.component_item.unit),
                "line_status": str(line.line_status),
            }
            for line in sorted(issue.lines, key=lambda x: x.line_id)
        ],
    }


def _issue_header(
    db: Session,
    issue: ProductionMaterialIssue,
    warehouse_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    product = issue.product
    item = product.item if product and product.item else None
    state = (
        getattr(product, "control_state", None)
        if product is not None
        else None
    )
    issue_status = str(issue.status or "")
    exported_ref = _clean_ref1c(issue.exported_ref1c)
    link = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.source_id == int(issue.issue_id),
            SyncLink.target_entity == STOCK_TRANSFER_ENTITY,
        )
        .one_or_none()
    )
    one_c_number = str(link.target_number or "") if link else ""
    can_assemble = bool(exported_ref) and issue_status != "posted"
    assemble_disabled_reason = ""
    if issue_status == "posted":
        assemble_disabled_reason = "Перемещение уже собрано"
    elif not exported_ref:
        assemble_disabled_reason = "Сначала выгрузите перемещение в 1С"
    warehouse_names = warehouse_names or _warehouse_name_lookup(
        db,
        [str(issue.source_warehouse_ref1c or ""), str(issue.warehouse_ref1c or "")],
    )
    return {
        "issue_id": int(issue.issue_id),
        "document_number": str(issue.document_number),
        "status": issue_status,
        "direction": str(issue.direction or "issue"),
        "product_id": int(issue.product_id),
        "order_id": int(issue.order_id),
        "order_number": str(issue.order.order_number or "") if issue.order else "",
        "order_prodplan_number": _prodplan_order_display_number(product, issue.order),
        "order_one_c_number": _order_one_c_number(db, issue.order),
        "order_source": str(issue.order.source or "1c") if issue.order else "",
        "order_ref1c": str(issue.order.order_ref1c or "") if issue.order and issue.order.order_ref1c else None,
        "item_id": int(product.item_id) if product else None,
        "item_name": str(item.item_name or "") if item else "",
        "item_article": str(item.item_article or "") if item else "",
        "item_code": str(item.item_code or "") if item else "",
        "quantity": _to_float(product.quantity) if product else 0.0,
        "remaining_qty": _to_float(product.remaining_qty) if product else 0.0,
        "unit": _unit_display(db, item.unit) if item else "",
        "warehouse_ref1c": str(issue.warehouse_ref1c or ""),
        "destination_warehouse_name": _warehouse_display_name(warehouse_names, issue.warehouse_ref1c),
        "source_warehouse_ref1c": str(issue.source_warehouse_ref1c or ""),
        "source_warehouse_name": _warehouse_display_name(warehouse_names, issue.source_warehouse_ref1c),
        "exported_ref1c": exported_ref,
        "one_c_number": one_c_number,
        "exported_at": _date_to_iso(issue.exported_at),
        "created_at": _date_to_iso(issue.created_at),
        "export_error": str(issue.export_error or ""),
        "can_assemble": can_assemble,
        "assemble_disabled_reason": assemble_disabled_reason,
        "line_status": str(state.status if state else ""),
        "issue_status": str(state.issue_status if state else ""),
        "lines_count": len(issue.lines or []),
    }


def list_material_issues(
    db: Session,
    *,
    status: Optional[str] = None,
    search: Optional[str] = None,
    source_warehouse_ref1c: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    query = (
        db.query(ProductionMaterialIssue)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionMaterialIssue.order_id)
        .join(ProductionProduct, ProductionProduct.product_id == ProductionMaterialIssue.product_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionMaterialIssue.product_id,
        )
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product)
            .joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.product)
            .joinedload(ProductionProduct.control_state),
            joinedload(ProductionMaterialIssue.lines),
        )
        .filter(ProductionMaterialIssue.direction == "issue")
        .filter(func.lower(func.coalesce(ProductionOrder.order_state_key, "")) != DONE_STATE_KEY)
        .filter(func.coalesce(ProductionOrderLineState.status, "").notin_(tuple(HIDDEN_ORDER_LINE_STATUSES)))
    )
    if status:
        query = query.filter(ProductionMaterialIssue.status == status)
    if search:
        like = f"%{search.strip()}%"
        query = (
            query.filter(
                (ProductionMaterialIssue.document_number.ilike(like))
                | (ProductionMaterialIssue.order.has(ProductionOrder.order_number.ilike(like)))
                | (ProductionProduct.item.has(Item.item_name.ilike(like)))
                | (ProductionProduct.item.has(Item.item_article.ilike(like)))
                | (ProductionProduct.item.has(Item.item_code.ilike(like)))
            )
        )
    source_warehouses = _issue_source_warehouse_options(db, query)
    clean_source_ref = _clean_ref1c(source_warehouse_ref1c)
    if clean_source_ref:
        query = query.filter(ProductionMaterialIssue.source_warehouse_ref1c == clean_source_ref)

    total = query.count()
    effective_limit = max(1, min(int(limit or 100), 500))
    effective_offset = max(0, int(offset or 0))
    rows = (
        query.order_by(ProductionMaterialIssue.created_at.desc(), ProductionMaterialIssue.issue_id.desc())
        .offset(effective_offset)
        .limit(effective_limit)
        .all()
    )
    warehouse_names = _warehouse_name_lookup(
        db,
        [
            ref
            for issue in rows
            for ref in (str(issue.source_warehouse_ref1c or ""), str(issue.warehouse_ref1c or ""))
        ],
    )
    return {
        "rows": [_issue_header(db, issue, warehouse_names) for issue in rows],
        "total": int(total),
        "limit": effective_limit,
        "offset": effective_offset,
        "source_warehouses": source_warehouses,
    }


def assemble_material_issue(
    db: Session,
    issue_id: int,
    *,
    allow_production: bool = False,
) -> Dict[str, Any]:
    issue = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.lines),
        )
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .one_or_none()
    )
    if issue is None:
        raise ValueError("Заявка на перемещение не найдена")
    ref_key = _clean_ref1c(issue.exported_ref1c)
    if not ref_key:
        raise ValueError("Перемещение ещё не выгружено в 1С")
    if str(issue.status or "") == "posted":
        return {"status": "ok", "issue_id": int(issue.issue_id), "already_posted": True}

    link = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.source_id == int(issue.issue_id),
            SyncLink.target_entity == STOCK_TRANSFER_ENTITY,
        )
        .one_or_none()
    )
    client = _create_odata_client(
        _load_odata_config(),
        OData1CClient,
        allow_production=allow_production,
        require_demo_base=True,
    )
    try:
        from .one_c_stock_transfer_export import (
            add_source_cells_to_payload,
            _build_header_payload,
            _collect_export_entries,
            _export_defaults,
        )

        entries, skipped = _collect_export_entries(db, [int(issue.issue_id)])
        if skipped:
            raise ValueError("; ".join(str(row.get("reason") or row) for row in skipped))
        if entries:
            payload = _build_header_payload(entries[0], _export_defaults(_load_odata_config()))
            if link is not None and link.target_number:
                payload["Number"] = str(link.target_number)
            add_source_cells_to_payload(client, entries[0], payload)
            patch = getattr(client, "patch", None)
            if patch is not None:
                patch(f"{STOCK_TRANSFER_ENTITY}(guid'{ref_key}')", payload)
        _post_document_operational(
            client,
            entity=STOCK_TRANSFER_ENTITY,
            ref_key=ref_key,
            unpost_first=True,
        )
    except Exception as exc:
        message = _clean_odata_error_message(exc)
        issue.export_error = message
        db.commit()
        raise ValueError(message) from exc

    issue.status = "posted"
    issue.export_error = None
    if link is not None:
        link.status = "posted"
        link.last_synced_at = datetime.now(timezone.utc)
    for line in issue.lines or []:
        line.issued_qty = line.required_qty
        line.line_status = "issued"
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == issue.product_id)
        .one_or_none()
    )
    if state is not None:
        if state.status in {"shortage", "partial", "ready", "to_move"}:
            state.status = "assembled"
        state.issue_status = "posted"
    db.commit()
    return {
        "status": "ok",
        "issue_id": int(issue.issue_id),
        "product_id": int(issue.product_id),
        "target_ref_key": ref_key,
    }


def build_issue_1c_payload(db: Session, issue_id: int) -> Dict[str, Any]:
    issue_data = get_issue(db, issue_id)
    issue = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.lines).joinedload(ProductionMaterialIssueLine.component_item),
        )
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .first()
    )
    if not issue:
        raise ValueError("Документ выдачи не найден")

    return {
        "Number": str(issue.document_number),
        "Date": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "Posted": False,
        "Комментарий": f"PRODPLAN: выдача под заказ {issue.order.order_number}, строка {issue.product.line_number or issue.product_id}",
        "ЗаказНаПроизводство_Key": str(issue.order.order_ref1c or ""),
        "Склад_Key": str(issue.warehouse_ref1c or ""),
        "Продукция_Key": str(issue.product.item.item_ref1c or "") if issue.product and issue.product.item else "",
        "ПродукцияКоличество": _to_float(issue.product.remaining_qty) or _to_float(issue.product.quantity),
        "Материалы": [
            {
                "LineNumber": idx + 1,
                "Номенклатура_Key": str(line.component_item.item_ref1c or ""),
                "Количество": _to_float(line.required_qty),
                "Единица": _unit_display(db, line.unit or line.component_item.unit),
            }
            for idx, line in enumerate(sorted(issue.lines, key=lambda x: x.line_id))
        ],
        "_prodplan": {
            "issue_id": issue_data["issue_id"],
            "document_number": issue_data["document_number"],
            "product_id": issue_data["product_id"],
        },
    }


def export_issue_to_1c(db: Session, issue_id: int, req: ODataSyncRequest) -> Dict[str, Any]:
    from .one_c_export_common import clean_ref1c

    issue = db.query(ProductionMaterialIssue).filter(ProductionMaterialIssue.issue_id == int(issue_id)).first()
    if not issue:
        raise ValueError("Документ выдачи не найден")

    # Idempotency: a document already created in 1C must not be POSTed again —
    # a repeat would create a duplicate Document_ПеремещениеЗапасов (a real
    # second stock transfer). Return the existing ref instead of re-posting.
    existing_ref = clean_ref1c(getattr(issue, "exported_ref1c", None))
    if existing_ref and not req.dry_run:
        return {
            "status": "already_exported",
            "issue_id": int(issue.issue_id),
            "document_number": str(issue.document_number),
            "exported_ref1c": existing_ref,
        }

    payload = build_issue_1c_payload(db, issue_id)

    if req.dry_run:
        return {
            "status": "dry_run",
            "entity_name": req.entity_name,
            "payload": payload,
        }

    from ..services.odata_client import OData1CClient

    client = OData1CClient(req.base_url, req.username, req.password, req.token)
    try:
        response = client.post(req.entity_name, payload, timeout=120)
        ref = str(response.get("Ref_Key") or response.get("ref") or response.get("Ref") or "")
        issue.status = "exported"
        issue.exported_ref1c = ref or None
        issue.exported_at = datetime.now(timezone.utc)
        issue.export_error = None
        state = (
            db.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id == issue.product_id)
            .first()
        )
        if state:
            state.issue_status = "exported"
        db.commit()
        return {
            "status": "ok",
            "issue_id": int(issue.issue_id),
            "document_number": str(issue.document_number),
            "exported_ref1c": ref,
            "response": response,
        }
    except Exception as e:
        issue.status = "error"
        issue.export_error = str(e)
        state = (
            db.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id == issue.product_id)
            .first()
        )
        if state:
            state.issue_status = "error"
        db.commit()
        raise
