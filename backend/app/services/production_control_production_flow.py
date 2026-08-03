from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

from ..models import (
    Employee,
    Item,
    Operation,
    ProductionManufacture,
    ProductionManufactureOperation,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionProduct,
    SpecComponent,
    SpecOperation,
    Specification,
    SyncLink,
)
from ..schemas import ODataSyncRequest
from .odata_config import load_odata_config
from .production_control_common import to_float as _to_float
from .production_control_domain import default_spec_id as _default_spec_id, ensure_state as _ensure_state
from .specification_sync import sync_specifications_from_odata
from .one_c_document_numbers import material_issue_number
from .one_c_manufacture_export import commanded_qty_by_product
from .one_c_piecework_export import PIECEWORK_ENTITY
from .production_output_truth import accepted_product_output
from .production_material_custody_events import append_material_issue_custody_event


# ---------------------------------------------------------------------------


def _refresh_product_spec_from_1c(db: Session, product: ProductionProduct) -> bool:
    """
    Pull the current 1C BOM for this line's specification before production.

    Operators may move materials after a technologist changed the 1C
    specification, while PRODPLAN still holds an older BOM snapshot. Production
    must be validated against the 1C-confirmed current BOM so quantity and
    composition changes in either direction are honored before the local
    reservation guard and 1C manufacture payload are built.
    """
    spec_id = _default_spec_id(db, product)
    if not spec_id:
        return False
    spec = db.query(Specification).filter(Specification.spec_id == int(spec_id)).first()
    spec_ref = str(getattr(spec, "spec_ref1c", None) or "").strip()
    if not spec_ref:
        return False

    config = load_odata_config()
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        return False

    try:
        sync_specifications_from_odata(
            db,
            ODataSyncRequest(
                base_url=base_url,
                entity_name="Catalog_Спецификации",
                username=config.get("username") or None,
                password=config.get("password") or None,
                token=config.get("token") or None,
                filter_query=f"Ref_Key eq guid'{spec_ref}'",
                select_fields=["Ref_Key", "Code", "Description", "ВидПроизводства_Key", "Состав", "Операции"],
                dry_run=False,
                zero_missing=False,
            ),
        )
        db.expire_all()
        return True
    except Exception as exc:
        raise ValueError(
            "Не удалось подтвердить актуальную спецификацию в 1С перед выпуском: "
            f"{exc}"
        ) from exc


def _ensure_workshop_reservation_covers(
    db: Session,
    product: ProductionProduct,
    qty: Optional[float] = None,
) -> None:
    """
    Block a production event that would consume more material than is
    currently held at the target workshop for this line.
    """
    from .production_control_domain import default_spec_id as _spec_for
    from .production_material_custody_projection import load_current_accepted_material_custody

    _refresh_product_spec_from_1c(db, product)
    spec_id = _spec_for(db, product)
    if not spec_id:
        return
    spec_rows = (
        db.query(SpecComponent)
        .filter(SpecComponent.spec_id == int(spec_id))
        .all()
    )
    if not spec_rows:
        return
    per_unit = {int(row.item_id): _to_float(row.quantity) for row in spec_rows}
    _, state = load_current_accepted_material_custody(
        db,
        consumer="production_output_material_guard",
    )
    reservation = state.for_product(int(product.product_id))

    shortfall_by_item: Dict[int, tuple] = {}
    for cid, per in per_unit.items():
        needed = per * qty
        if needed <= 1e-9:
            continue
        held = reservation.at_workshop.get(cid, 0.0)
        if held + 1e-6 < needed:
            shortfall_by_item[cid] = (needed, held)
    if shortfall_by_item:
        names = {
            int(item.item_id): str(item.item_name or item.item_code or item.item_id)
            for item in db.query(Item).filter(Item.item_id.in_(shortfall_by_item.keys())).all()
        }
        shortfalls = [
            f"{names.get(cid, cid)}: нужно {needed:g}, удержано на участке {held:g}"
            for cid, (needed, held) in sorted(shortfall_by_item.items())
        ]
        raise ValueError(
            "Недостаточно компонентов, удержанных на участке для этой строки: "
            + "; ".join(shortfalls[:10])
            + ". Сначала проведите перемещение недостающего или зафиксируйте удержание на участке."
        )


def _piecework_order_is_in_1c(db: Session, manufacture_id: int) -> bool:
    """True when this выпуск already has its Document_СдельныйНаряд in 1C."""
    link = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "piecework",
            SyncLink.source_id == int(manufacture_id),
            SyncLink.target_entity == PIECEWORK_ENTITY,
        )
        .one_or_none()
    )
    return bool(
        link is not None
        and str(link.status or "") == "success"
        and str(link.target_ref_key or "").strip()
    )


def _resumable_manufacture(db: Session, product_id: int):
    """
    An unfinished «Произвести» of this line that must be continued, not repeated.

    Per .docs/one_c_export_from_prodplan.md п.8 ("если шаг цепочки упал, уже
    созданные документы не дублируются при повторном нажатии"), a выпуск whose
    Document_СборкаЗапасов already lives in 1C but which never got its
    Document_СдельныйНаряд is a half-finished chain: the next press must roll it
    forward. Returns (manufacture, reason) or None.
    """
    rows = (
        db.query(ProductionManufacture)
        .filter(ProductionManufacture.product_id == int(product_id))
        .filter(ProductionManufacture.status.in_(("draft", "exported", "error")))
        .order_by(ProductionManufacture.manufacture_id.asc())
        .all()
    )
    for manufacture in rows:
        if not str(manufacture.exported_ref1c or "").strip():
            continue
        if _piecework_order_is_in_1c(db, int(manufacture.manufacture_id)):
            continue
        if str(manufacture.status or "").lower() == "error":
            reason = (
                "СборкаЗапасов уже создана в 1С, но не завершена "
                f"({manufacture.export_error or 'ошибка выгрузки'}) — повторяем выгрузку"
            )
        else:
            reason = "СборкаЗапасов уже проведена в 1С, но СдельныйНаряд не создан — докатываем цепочку"
        return manufacture, reason
    return None


def produce_line(
    db: Session,
    product_id: int,
    *,
    qty: Optional[float] = None,
    executor: Optional[str] = None,
    operation_executors: Optional[List[Dict[str, Any]]] = None,
    comment: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Record an operator command to create production documents in 1C.

    Effects:
    - Creates a ProductionManufacture command row with executors.
    - Does not record physical execution or mutate produced/remaining caches.

    The command is exported as Document_СборкаЗапасов and
    Document_СдельныйНаряд. Only the read-back posting register imported into
    Item Ledger may change factual execution through canonical FIFO.
    """
    requested_qty = float(qty) if qty is not None else None
    if requested_qty is not None and requested_qty <= 0:
        raise ValueError("qty должен быть положительным")

    product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.product_id == int(product_id))
        .one_or_none()
    )
    if product is None:
        raise ValueError(f"product_id={product_id}: строка заказа не найдена")

    order_quantity = _to_float(product.quantity)
    commanded_before = commanded_qty_by_product(db, [int(product.product_id)]).get(
        int(product.product_id), 0.0
    )
    accepted_output = accepted_product_output(product)

    # Resume before anything else: an unfinished выпуск already owns a 1C
    # Document_СборкаЗапасов, so a second press must finish that chain instead
    # of creating a duplicate assembly (or dying on the "всё уже скомандовано"
    # guard when the наряд step failed).
    resumable = _resumable_manufacture(db, int(product.product_id))
    if resumable is not None:
        manufacture, resume_reason = resumable
        state = _ensure_state(db, product)
        db.commit()
        return {
            "status": "resumed_pending_1c_fact",
            "resumed": True,
            "resume_reason": resume_reason,
            "manufacture_id": int(manufacture.manufacture_id),
            "product_id": int(product.product_id),
            "order_id": int(product.order_id),
            "qty": float(_to_float(manufacture.qty)),
            "requested_qty": (
                float(requested_qty)
                if requested_qty is not None
                else float(_to_float(manufacture.qty))
            ),
            "produced_qty_total": float(accepted_output.produced_qty),
            "remaining_qty": float(accepted_output.remaining_qty),
            "commanded_qty_total": float(commanded_before),
            "command_remaining_qty": float(
                min(
                    max(order_quantity - commanded_before, 0.0),
                    accepted_output.remaining_qty,
                )
            ),
            "fact_pending": True,
            "line_status": state.status,
        }

    # A legacy/imported 1C fact may have no corresponding local command row,
    # while a pending local command may not yet be a physical fact. The safe
    # executable quantity is bounded by both independent ceilings.
    command_remaining = min(
        max(0.0, order_quantity - commanded_before),
        float(accepted_output.remaining_qty),
    )
    if command_remaining <= 1e-9:
        raise ValueError(
            "По этой строке уже создана исполнительная команда на весь объём"
        )
    qty_f = command_remaining if requested_qty is None else requested_qty
    if qty_f - command_remaining > 1e-6:
        raise ValueError(
            "qty превышает остаток, ещё не переданный в исполнительные документы"
        )

    material_issue = (
        db.query(ProductionMaterialIssue)
        .filter(
            ProductionMaterialIssue.product_id == int(product.product_id),
            ProductionMaterialIssue.direction.in_(("issue", "in_place")),
            ProductionMaterialIssue.status.in_(("posted", "issued")),
        )
        .order_by(ProductionMaterialIssue.issue_id.desc())
        .first()
    )
    if material_issue is None:
        raise ValueError(
            "Нельзя создать выпуск без проведённого перемещения материалов по этой строке"
        )

    _ensure_workshop_reservation_covers(db, product, qty_f)

    manufacture = ProductionManufacture(
        product_id=int(product.product_id),
        order_id=int(product.order_id),
        qty=qty_f,
        executor=(str(executor).strip() if executor else None) or None,
        comment=(str(comment).strip() if comment else None) or None,
        status="draft",
    )
    db.add(manufacture)
    db.flush()

    executor_rows = [row for row in (operation_executors or []) if isinstance(row, dict)]
    for idx, row in enumerate(executor_rows, start=1):
        employee_ref = str(row.get("employee_ref1c") or "").strip()
        if not employee_ref:
            continue
        employee = (
            db.query(Employee)
            .filter(Employee.employee_ref1c == employee_ref)
            .filter(Employee.deletion_mark.is_(False))
            .one_or_none()
        )
        if employee is None:
            raise ValueError(f"employee_ref1c={employee_ref}: исполнитель не найден")
        spec_operation_id = row.get("spec_operation_id")
        operation_id = row.get("operation_id")
        spec_operation = None
        if spec_operation_id is not None:
            spec_operation = (
                db.query(SpecOperation)
                .filter(SpecOperation.spec_operation_id == int(spec_operation_id))
                .one_or_none()
            )
            if spec_operation is None:
                raise ValueError(f"spec_operation_id={spec_operation_id}: операция спецификации не найдена")
            operation_id = int(spec_operation.operation_id)
        if operation_id is None:
            raise ValueError("operation_id обязателен для исполнителя операции")
        operation = db.query(Operation).filter(Operation.operation_id == int(operation_id)).one_or_none()
        if operation is None:
            raise ValueError(f"operation_id={operation_id}: операция не найдена")
        db.add(ProductionManufactureOperation(
            manufacture_id=int(manufacture.manufacture_id),
            spec_operation_id=int(spec_operation.spec_operation_id) if spec_operation else None,
            operation_id=int(operation.operation_id),
            line_number=int(row.get("line_number") or idx),
            employee_ref1c=str(employee.employee_ref1c),
            employee_name=str(employee.employee_name or ""),
            employee_type=str(getattr(employee, "employee_type", None) or "employee"),
        ))

    state = _ensure_state(db, product)
    db.commit()

    return {
        "status": "pending_1c_fact",
        "resumed": False,
        "manufacture_id": int(manufacture.manufacture_id),
        "product_id": int(product.product_id),
        "order_id": int(product.order_id),
        "qty": float(qty_f),
        "produced_qty_total": float(accepted_output.produced_qty),
        "remaining_qty": float(accepted_output.remaining_qty),
        "commanded_qty_total": float(commanded_before + qty_f),
        "command_remaining_qty": float(max(command_remaining - qty_f, 0.0)),
        "fact_pending": True,
        "line_status": state.status,
    }


def rollback_local_manufacture(db: Session, manufacture_id: int) -> Dict[str, Any]:
    """
    Undo a local manufacture that was created before its 1C export failed.
    Only non-exported draft rows may be rolled back.
    """
    manufacture = (
        db.query(ProductionManufacture)
        .filter(ProductionManufacture.manufacture_id == int(manufacture_id))
        .one_or_none()
    )
    if manufacture is None:
        raise ValueError(f"manufacture_id={manufacture_id}: выпуск не найден")
    if getattr(manufacture, "exported_ref1c", None):
        raise ValueError("Нельзя откатить выпуск, уже выгруженный в 1С")
    if manufacture.status == "exported":
        raise ValueError("Нельзя откатить выгруженный выпуск")

    product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.product_id == int(manufacture.product_id))
        .one_or_none()
    )
    if product is None:
        raise ValueError(f"product_id={manufacture.product_id}: строка заказа не найдена")

    state = _ensure_state(db, product)
    accepted_output = accepted_product_output(product)

    db.delete(manufacture)
    db.commit()

    return {
        "status": "rolled_back",
        "manufacture_id": int(manufacture_id),
        "product_id": int(product.product_id),
        "produced_qty_total": float(accepted_output.produced_qty),
        "remaining_qty": float(accepted_output.remaining_qty),
        "line_status": state.status,
    }


# ---------------------------------------------------------------------------
# Reverse transfer: return leftover components from workshop to source.
# Plan rule (Следующие этапы #6): "При частичном выпуске создавать обратное
# перемещение лишних компонентов на исходные склады."
# ---------------------------------------------------------------------------


def _outgoing_issues_for_product(
    db: Session, product_id: int
) -> List[ProductionMaterialIssue]:
    """
    Outgoing material issues that actually delivered (or are reported as
    delivered) into the workshop. 'cancelled' / 'draft' / 'requested' are
    excluded вЂ” those haven't physically moved yet.
    """
    return (
        db.query(ProductionMaterialIssue)
        .options(joinedload(ProductionMaterialIssue.lines))
        .filter(ProductionMaterialIssue.product_id == int(product_id))
        .filter(ProductionMaterialIssue.direction.in_(("issue", "in_place")))
        .filter(ProductionMaterialIssue.status.in_(("exported", "posted", "issued")))
        .all()
    )


def _next_return_number(db: Session) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"MR-{today}-"
    count = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.document_number.like(f"{prefix}%"))
        .count()
    )
    return f"{prefix}{count + 1:04d}"


def return_leftover_components(
    db: Session,
    product_id: int,
    *,
    initiated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compute leftover components for a partially-produced line and create a
    return-transfer ProductionMaterialIssue (direction='return') with swapped
    source / destination warehouses.

    Steps:
    1. Find original outgoing material_issues (direction='issue', status in
       exported/posted/issued) for this product.
    2. Sum issued qty per component across them.
    3. Compute consumed = produced_qty * spec_qty_per_unit per component.
    4. leftover = max(0, issued - consumed). Skip components with no leftover.
    5. Create one ProductionMaterialIssue with direction='return' + swapped
       warehouses + leftover-qty lines.

    Idempotency: if an active (draft|requested) return-issue already exists
    for the product, return it under `reused=True`. The unique index on
    production_material_issues was scoped to direction='issue' so a return
    draft does not conflict with an outgoing one.

    Returns:
      {status, return_issue_id?, document_number?, reused?, lines: [...],
       source_warehouse_ref1c?, destination_warehouse_ref1c?, skipped_reason?}
    """
    product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.product_id == int(product_id))
        .one_or_none()
    )
    if product is None:
        raise ValueError(f"product_id={product_id}: строка заказа не найдена")

    produced_qty = _to_float(product.produced_qty)
    if produced_qty <= 0:
        return {
            "status": "skipped",
            "skipped_reason": "produced_qty=0: нечего возвращать без выпуска",
            "lines": [],
        }

    existing_active = (
        db.query(ProductionMaterialIssue)
        .filter(
            ProductionMaterialIssue.product_id == int(product_id),
            ProductionMaterialIssue.direction == "return",
            ProductionMaterialIssue.status.in_(("draft", "requested")),
        )
        .order_by(ProductionMaterialIssue.issue_id.desc())
        .first()
    )
    if existing_active is not None:
        return {
            "status": "ok",
            "reused": True,
            "return_issue_id": int(existing_active.issue_id),
            "document_number": str(existing_active.document_number),
            "lines": [],
        }

    outgoing = _outgoing_issues_for_product(db, int(product_id))
    if not outgoing:
        return {
            "status": "skipped",
            "skipped_reason": (
                "Нет выгруженных в 1С исходящих перемещений по этой строке"
            ),
            "lines": [],
        }

    issued_by_component: Dict[int, float] = {}
    unit_by_component: Dict[int, str] = {}
    spec_by_component: Dict[int, Optional[int]] = {}
    for issue in outgoing:
        for ln in issue.lines:
            cid = int(ln.component_item_id)
            issued_by_component[cid] = issued_by_component.get(cid, 0.0) + _to_float(
                ln.required_qty
            )
            if ln.unit:
                unit_by_component.setdefault(cid, str(ln.unit))
            if ln.source_spec_id and cid not in spec_by_component:
                spec_by_component[cid] = int(ln.source_spec_id)

    spec_id = _default_spec_id(db, product)
    qty_per_unit: Dict[int, float] = {}
    if spec_id:
        rows = (
            db.query(SpecComponent.item_id, SpecComponent.quantity)
            .filter(SpecComponent.spec_id == spec_id)
            .all()
        )
        qty_per_unit = {int(iid): _to_float(q) for iid, q in rows}

    leftovers: Dict[int, Dict[str, Any]] = {}
    for cid, issued in issued_by_component.items():
        per_unit = qty_per_unit.get(cid, 0.0)
        consumed = produced_qty * per_unit
        leftover = issued - consumed
        if leftover <= 1e-9:
            continue
        leftovers[cid] = {
            "component_item_id": cid,
            "issued_qty": float(issued),
            "consumed_qty": float(consumed),
            "qty_per_unit": float(per_unit),
            "leftover_qty": float(leftover),
            "unit": unit_by_component.get(cid),
            "source_spec_id": spec_by_component.get(cid),
        }

    # In-place claims release without a 1C document: those components never
    # physically moved, dropping the local reservation puts them back into
    # the free workshop pool. Only the remainder needs a physical return.
    released_in_place: List[Dict[str, Any]] = []
    for issue in sorted(outgoing, key=lambda i: i.issue_id, reverse=True):
        is_zero_distance_issue = (
            str(issue.direction or "") == "issue"
            and str(issue.source_warehouse_ref1c or "").strip()
            == str(issue.warehouse_ref1c or "").strip()
        )
        if str(issue.direction or "") != "in_place" and not is_zero_distance_issue:
            continue
        for line in sorted(issue.lines or [], key=lambda l: l.line_id, reverse=True):
            cid = int(line.component_item_id)
            entry = leftovers.get(cid)
            if entry is None or entry["leftover_qty"] <= 1e-9:
                continue
            held = _to_float(line.issued_qty)
            take = min(held, entry["leftover_qty"])
            if take <= 1e-9:
                continue
            line.issued_qty = held - take
            line.required_qty = max(0.0, _to_float(line.required_qty) - take)
            entry["leftover_qty"] = float(entry["leftover_qty"] - take)
            warehouse = str(issue.warehouse_ref1c or "").strip()
            if warehouse:
                append_material_issue_custody_event(
                    db,
                    issue=issue,
                    line=line,
                    delta_qty=-take,
                    source_kind="terminal_release",
                    location_kind="workshop",
                    warehouse_ref1c=warehouse,
                    source_ref1c=str(issue.source_warehouse_ref1c or ""),
                )
            released_in_place.append(
                {"component_item_id": cid, "released_qty": float(take)}
            )

    return_lines: List[Dict[str, Any]] = [
        entry for entry in leftovers.values() if entry["leftover_qty"] > 1e-9
    ]

    if not return_lines:
        if released_in_place:
            db.commit()
            return {
                "status": "ok",
                "reused": False,
                "released_in_place": released_in_place,
                "lines": [],
            }
        return {
            "status": "skipped",
            "skipped_reason": "Нет компонентов с положительным остатком",
            "lines": [],
        }

    physical_outgoing = [
        issue for issue in outgoing if str(issue.direction or "") == "issue"
    ]
    if not physical_outgoing:
        db.commit()
        return {
            "status": "skipped",
            "skipped_reason": (
                "Остаток числится только по комплектации с участка — "
                "физический возврат не требуется"
            ),
            "released_in_place": released_in_place,
            "lines": return_lines,
        }
    latest_outgoing = max(physical_outgoing, key=lambda i: i.issue_id)
    return_source = str(latest_outgoing.warehouse_ref1c or "") or None
    return_dest = str(latest_outgoing.source_warehouse_ref1c or "") or None

    new_issue = ProductionMaterialIssue(
        document_number="",
        product_id=int(product.product_id),
        order_id=int(product.order_id),
        status="draft",
        direction="return",
        warehouse_ref1c=return_dest,
        source_warehouse_ref1c=return_source,
        initiated_by=initiated_by,
    )
    db.add(new_issue)
    db.flush()
    new_issue.document_number = material_issue_number(db, new_issue)
    for ln in return_lines:
        db.add(
            ProductionMaterialIssueLine(
                issue_id=int(new_issue.issue_id),
                component_item_id=int(ln["component_item_id"]),
                required_qty=float(ln["leftover_qty"]),
                issued_qty=0.0,
                unit=ln.get("unit"),
                source_spec_id=ln.get("source_spec_id"),
                line_status="planned",
            )
        )
    db.commit()

    return {
        "status": "ok",
        "reused": False,
        "return_issue_id": int(new_issue.issue_id),
        "document_number": new_issue.document_number,
        "source_warehouse_ref1c": return_source,
        "destination_warehouse_ref1c": return_dest,
        "released_in_place": released_in_place,
        "lines": return_lines,
    }
