"""Export internal MRP-source ProductionOrders to 1C as Document_ЗаказНаПроизводство.

Pattern: mirrors backend/app/services/one_c_purchase_order_export.py.
Documentation: .docs/one_c_export_from_prodplan.md.

Safety rules from the doc are enforced on top of the call site:
1. Default `dry_run=True`; explicit dry_run=False is required to write.
2. Always send `Posted=false`, then immediately conduct the created order
   through the standard 1C `Post?PostingModeOperational=true` command.
3. Idempotency: skip orders that already have a successful sync_link OR a
   non-empty `production_orders.order_ref1c` (it gets stamped from the
   1C response on first successful export).

Only MRP-source production_orders (source='mrp') are eligible. 1C-synced
orders (source='1c') already exist in 1C — we wouldn't re-export them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ..models import (
    Item,
    Operation,
    PaintWeldChainLink,
    ProductionMaterialIssue,
    ProductionOrder,
    PlanningRun,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    Specification,
    SpecOperation,
    SyncLink,
    Unit,
)
from .workshop_resolution import (
    diagnose_product,
    resolve_workshop_for_product,
    spec_id_for_product,
    warehouse_binding_for_workshop,
)
from .one_c_export_common import (
    DEFAULT_ORGANIZATION_REF1C,
    DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C,
    add_unit_payload as _add_unit_payload,
    clean_ref1c as _clean_ref1c,
    config_ref1c as _config_ref1c,
    create_odata_client as _create_odata_client,
    current_1c_datetime as _current_1c_datetime,
    find_sync_link as _find_sync_link,
    find_document_by_origin as _find_document_by_origin,
    fmt_1c_datetime as _fmt_1c_datetime,
    add_origin_marker as _add_origin_marker,
    origin_token as _origin_token,
    payload_hash as _payload_hash,
    post_document_operational as _post_document_operational,
    post_export_entries as _post_export_entries,
    upsert_sync_link as _upsert_sync_link,
)
from .one_c_document_numbers import production_order_number
from .mrp_mutation_guard import MrpMutationLineageError, require_materialized_orders
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient
from .bom_specification_resolver import BomSpecificationResolver


PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"
PRODUCTION_ORDER_PRODUCTS_ENTITY = "Document_ЗаказНаПроизводство_Продукция"
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"


@dataclass
class ProductionOrderExportLine:
    line_number: int
    item_id: int
    item_ref1c: str
    item_name: str
    item_article: str
    unit_ref1c: Optional[str]
    qty: float
    characteristic_ref1c: Optional[str] = None
    spec_ref1c: Optional[str] = None
    structural_unit_ref1c: Optional[str] = None


@dataclass
class ProductionOrderExportMaterial:
    line_number: int
    component_item_id: int
    item_ref1c: str
    unit_ref1c: Optional[str]
    qty: float
    spec_ref1c: Optional[str] = None
    reserve_structural_unit_ref1c: Optional[str] = None


@dataclass
class ProductionOrderExportOperation:
    line_number: int
    operation_ref1c: str
    unit_ref1c: Optional[str]
    qty: float
    time_norm: float
    norm_hours: float
    structural_unit_ref1c: Optional[str] = None
    product_link_key: Optional[int] = None


@dataclass
class ProductionOrderExportEntry:
    order_id: int
    number: str
    source_planned_order_id: Optional[int] = None
    source_run_id: Optional[int] = None
    ledger_generation_id: Optional[int] = None
    freeze_version: Optional[int] = None
    lines: List[ProductionOrderExportLine] = field(default_factory=list)
    materials: List[ProductionOrderExportMaterial] = field(default_factory=list)
    operations: List[ProductionOrderExportOperation] = field(default_factory=list)
    reserve_structural_unit_ref1c: Optional[str] = None
    product_structural_unit_ref1c: Optional[str] = None
    planned_start_date: Optional[Any] = None
    planned_finish_date: Optional[Any] = None
    # The 1C document timestamp participates in the payload fingerprint.  It
    # must therefore come from durable order data, never from the retry clock.
    document_date: Optional[Any] = None
    target_ref_key: Optional[str] = None
    status: str = "planned"  # planned | created | existing | error | skipped
    error: Optional[str] = None
    reason: Optional[str] = None  # human-readable explanation for skipped/error
    origin_token: Optional[str] = None
    unpost_before_patch: bool = False


@dataclass
class ProductionOrderCloseEntry:
    order_id: int
    number: str
    order_ref1c: str
    source_run_id: Optional[int] = None
    ledger_generation_id: Optional[int] = None
    freeze_version: Optional[int] = None
    target_ref_key: Optional[str] = None
    status: str = "planned"
    error: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class ProductionOrderExportDefaults:
    organization_ref1c: str = ""
    structural_unit_ref1c: str = ""
    product_structural_unit_ref1c: str = ""
    operation_structural_unit_ref1c: str = ""


@dataclass
class ProductionOrderCloseDefaults:
    close_state_ref1c: str = ""
    close_variant_ref1c: str = ""


def _export_defaults(config: Dict[str, Any]) -> ProductionOrderExportDefaults:
    structural_unit = _config_ref1c(
        config,
        "default_production_structural_unit_ref1c",
        DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C,
    )
    return ProductionOrderExportDefaults(
        organization_ref1c=_config_ref1c(
            config,
            "default_organization_ref1c",
            DEFAULT_ORGANIZATION_REF1C,
        ),
        structural_unit_ref1c=structural_unit,
        product_structural_unit_ref1c=_config_ref1c(
            config,
            "default_product_structural_unit_ref1c",
            structural_unit,
        ),
        operation_structural_unit_ref1c=_config_ref1c(
            config,
            "default_operation_structural_unit_ref1c",
            structural_unit,
        ),
    )


def _close_defaults(config: Dict[str, Any]) -> ProductionOrderCloseDefaults:
    return ProductionOrderCloseDefaults(
        close_state_ref1c=_config_ref1c(config, "default_production_order_done_state_ref1c"),
        close_variant_ref1c=_config_ref1c(config, "default_production_order_done_variant_ref1c"),
    )


def _short_order_number(order_id: int, run_id: Optional[int]) -> str:
    """
    Short, recognizable, unique-per-MRP-order number that fits 1C's Number
    column (per plan: 1C truncates long strings).
    Format: PP{run_id:04d}{order_id:05d}. Total length 11 chars, well under
    1C's typical Number limit. Collisions impossible while order_id is < 10^5
    within a single run_id.
    """
    run_part = (int(run_id) if run_id is not None else 0) % 10000
    return f"PP{run_part:04d}{int(order_id) % 100000:05d}"


def _workshop_warehouse_refs(db: Session, workshop_id: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    binding = warehouse_binding_for_workshop(db, workshop_id)
    if not binding:
        return None, None
    workshop_ref = _clean_ref1c(binding.warehouse_ref1c) or None
    production_ref = _clean_ref1c(binding.production_warehouse_ref1c) or None
    return workshop_ref, production_ref


def _active_issue_for_product(db: Session, product_id: int) -> Optional[ProductionMaterialIssue]:
    return (
        db.query(ProductionMaterialIssue)
        .filter(
            ProductionMaterialIssue.product_id == int(product_id),
            ProductionMaterialIssue.direction == "issue",
        )
        .order_by(ProductionMaterialIssue.issue_id.desc())
        .first()
    )


def _paint_weld_next_stage_destination_ref(
    db: Session, order_id: int
) -> Optional[str]:
    """Return the painted workshop warehouse for a welded chain order.

    A welded product is consumed by the painted order, so its physical output
    must land at the next executor (powder coating), not at the welded
    workshop's generic finished-goods warehouse.  The chain link is the
    canonical sequence owner; the painted product's workshop binding owns the
    concrete 1C structural-unit reference.
    """
    link = (
        db.query(PaintWeldChainLink)
        .filter(PaintWeldChainLink.welded_order_id == int(order_id))
        .one_or_none()
    )
    if link is None:
        return None
    painted_products = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.order_id == int(link.painted_order_id))
        .all()
    )
    if len(painted_products) != 1:
        raise ValueError(
            f"paint/weld chain painted order {int(link.painted_order_id)} "
            "does not have exactly one product"
        )
    painted_product = painted_products[0]
    painted_spec_id = spec_id_for_product(db, painted_product)
    painted_workshop_id = resolve_workshop_for_product(
        db, painted_product, spec_id=painted_spec_id
    )
    binding = warehouse_binding_for_workshop(db, painted_workshop_id)
    destination_ref = _clean_ref1c(binding.warehouse_ref1c) if binding else ""
    if not destination_ref:
        raise ValueError(
            f"paint/weld chain painted order {int(link.painted_order_id)} "
            "has no recipient workshop warehouse"
        )
    return destination_ref


def _materials_for_spec(
    db: Session,
    *,
    spec_id: Optional[int],
    order_qty: float,
    reserve_structural_unit_ref1c: Optional[str],
) -> List[ProductionOrderExportMaterial]:
    if not spec_id or order_qty <= 0:
        return []
    spec = db.query(Specification).filter(Specification.spec_id == int(spec_id)).first()
    spec_ref = _clean_ref1c(spec.spec_ref1c) if spec else None
    rows = (
        db.query(SpecComponent, Item)
        .join(Item, Item.item_id == SpecComponent.item_id)
        .filter(SpecComponent.spec_id == int(spec_id))
        .order_by(Item.item_name.asc(), SpecComponent.component_id.asc())
        .all()
    )
    spec_resolver = BomSpecificationResolver(db)
    result: List[ProductionOrderExportMaterial] = []
    for idx, (comp, item) in enumerate(rows, start=1):
        item_ref = _clean_ref1c(item.item_ref1c)
        qty = float(comp.quantity or 0.0) * float(order_qty)
        if not item_ref or qty <= 0:
            continue
        result.append(
            ProductionOrderExportMaterial(
                line_number=idx,
                component_item_id=int(item.item_id),
                item_ref1c=item_ref,
                unit_ref1c=_clean_ref1c(item.unit) or None,
                qty=qty,
                spec_ref1c=(
                    spec_resolver.child_spec_ref1c(comp)
                    or spec_ref
                    or None
                ),
                reserve_structural_unit_ref1c=reserve_structural_unit_ref1c,
            )
        )
    return result


def _operations_for_spec(
    db: Session,
    *,
    spec_id: Optional[int],
    order_qty: float,
    product_unit_ref1c: Optional[str],
    structural_unit_ref1c: Optional[str],
    product_link_key: int,
) -> List[ProductionOrderExportOperation]:
    if not spec_id or order_qty <= 0:
        return []
    rows = (
        db.query(SpecOperation, Operation)
        .join(Operation, Operation.operation_id == SpecOperation.operation_id)
        .filter(SpecOperation.spec_id == int(spec_id))
        .order_by(SpecOperation.spec_operation_id.asc())
        .all()
    )
    result: List[ProductionOrderExportOperation] = []
    for idx, (spec_op, op) in enumerate(rows, start=1):
        op_ref = _clean_ref1c(op.operation_ref1c)
        if not op_ref:
            continue
        time_norm = float(spec_op.time_norm if spec_op.time_norm is not None else op.time_norm or 0.0)
        result.append(
            ProductionOrderExportOperation(
                line_number=idx,
                operation_ref1c=op_ref,
                unit_ref1c=product_unit_ref1c,
                qty=float(order_qty),
                time_norm=time_norm,
                norm_hours=float(order_qty) * time_norm,
                structural_unit_ref1c=structural_unit_ref1c,
                product_link_key=int(product_link_key),
            )
        )
    return result


def _existing_link(db: Session, order_id: int) -> Optional[SyncLink]:
    return _find_sync_link(
        db,
        SyncLink,
        source_doctype="production_order",
        source_id=int(order_id),
        target_entity=PRODUCTION_ORDER_ENTITY,
    )


def _combine_planned_date_with_time(value: Optional[Any], time_source: str) -> Optional[str]:
    if value is None:
        return None
    try:
        source_dt = datetime.fromisoformat(str(time_source))
    except Exception:
        source_dt = datetime.now().replace(microsecond=0)
    if isinstance(value, datetime):
        planned_date = value.date()
    elif isinstance(value, date):
        planned_date = value
    else:
        try:
            planned_date = date.fromisoformat(str(value)[:10])
        except Exception:
            return None
    return datetime.combine(planned_date, source_dt.time()).replace(microsecond=0).isoformat()


def _collect_export_entries(
    db: Session, order_ids: List[int]
) -> Tuple[List[ProductionOrderExportEntry], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Load production_orders + their single ProductionProduct line + Item lookup.
    Returns (entries, skipped, warnings): skipped contains diagnostic dicts
    for orders that can't be exported (wrong source, missing item ref, etc);
    warnings are non-blocking routing problems (workshop not resolved).
    """
    entries: List[ProductionOrderExportEntry] = []
    skipped: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    ids = [int(x) for x in order_ids if x is not None]
    if not ids:
        return entries, skipped, warnings

    rows = (
        db.query(ProductionOrder)
        .options(joinedload(ProductionOrder.products).joinedload(ProductionProduct.item))
        .filter(ProductionOrder.order_id.in_(ids))
        .all()
    )
    found_ids = {int(o.order_id) for o in rows}
    for missing_id in [x for x in ids if x not in found_ids]:
        skipped.append({"order_id": missing_id, "reason": "ProductionOrder не найден"})

    for order in rows:
        if str(order.source or "1c").lower() != "mrp":
            skipped.append(
                {
                    "order_id": int(order.order_id),
                    "reason": f"source='{order.source}', экспортируем только MRP-source",
                }
            )
            continue
        if bool(order.deletion_mark):
            skipped.append({"order_id": int(order.order_id), "reason": "deletion_mark=true"})
            continue

        lines: List[ProductionOrderExportLine] = []
        materials: List[ProductionOrderExportMaterial] = []
        operations: List[ProductionOrderExportOperation] = []
        reserve_ref: Optional[str] = None
        product_structural_unit_ref: Optional[str] = None
        start_dates: List[Any] = []
        finish_dates: List[Any] = []
        chain_destination_ref = _paint_weld_next_stage_destination_ref(
            db, int(order.order_id)
        )
        for product in order.products or []:
            item = product.item
            ref1c = _clean_ref1c(item.item_ref1c) if item else ""
            if not ref1c:
                skipped.append(
                    {
                        "order_id": int(order.order_id),
                        "reason": f"item_id={product.item_id}: пустой item_ref1c, "
                        "нельзя сопоставить с номенклатурой 1С",
                    }
                )
                lines = []
                break
            spec_id = spec_id_for_product(db, product)
            workshop_id = resolve_workshop_for_product(db, product, spec_id=spec_id)
            if workshop_id is None:
                # The order is still exported (1C needs it even without a
                # reserve unit), but the routing gap must be visible.
                diagnosis = diagnose_product(db, product)
                warnings.append(
                    {
                        "order_id": int(order.order_id),
                        "product_id": int(product.product_id),
                        "reason_code": diagnosis.reason_code,
                        "message": f"{diagnosis.reason_text}. {diagnosis.recommendation}",
                    }
                )
            issue = _active_issue_for_product(db, int(product.product_id))
            workshop_warehouse_ref, production_warehouse_ref = _workshop_warehouse_refs(db, workshop_id)
            material_destination_ref = (
                _clean_ref1c(issue.warehouse_ref1c) if issue else None
            ) or workshop_warehouse_ref
            product_destination_ref = (
                chain_destination_ref
                or production_warehouse_ref
                or material_destination_ref
            )
            product_reserve_ref = material_destination_ref
            if product_reserve_ref and not reserve_ref:
                reserve_ref = product_reserve_ref
            if product_destination_ref and not product_structural_unit_ref:
                product_structural_unit_ref = product_destination_ref
            state = (
                db.query(ProductionOrderLineState)
                .filter(ProductionOrderLineState.product_id == int(product.product_id))
                .first()
            )
            if state and state.planned_start_date:
                start_dates.append(state.planned_start_date)
            if state and state.planned_finish_date:
                finish_dates.append(state.planned_finish_date)
            product_unit_ref = _clean_ref1c(item.unit) or None
            product_qty = float(product.quantity or 0.0)
            product_link_key = int(product.line_number or len(lines) + 1)
            lines.append(
                ProductionOrderExportLine(
                    line_number=product_link_key,
                    item_id=int(product.item_id),
                    item_ref1c=ref1c,
                    item_name=str(item.item_name or ""),
                    item_article=str(item.item_article or ""),
                    unit_ref1c=product_unit_ref,
                    qty=product_qty,
                    characteristic_ref1c=_clean_ref1c(product.characteristic_ref1c) or None,
                    spec_ref1c=(
                        _clean_ref1c(
                            getattr(
                                db.query(Specification)
                                .filter(Specification.spec_id == int(spec_id))
                                .first(),
                                "spec_ref1c",
                                None,
                            )
                        )
                        if spec_id
                        else None
                    ),
                    structural_unit_ref1c=product_destination_ref,
                )
            )
            materials.extend(
                _materials_for_spec(
                    db,
                    spec_id=spec_id,
                    order_qty=product_qty,
                    reserve_structural_unit_ref1c=product_reserve_ref,
                )
            )
            operations.extend(
                _operations_for_spec(
                    db,
                    spec_id=spec_id,
                    order_qty=product_qty,
                    product_unit_ref1c=product_unit_ref,
                    structural_unit_ref1c=product_destination_ref,
                    product_link_key=product_link_key,
                )
            )

        if not lines:
            continue

        entries.append(
            ProductionOrderExportEntry(
                order_id=int(order.order_id),
                number=production_order_number(order),
                source_planned_order_id=None,
                source_run_id=int(order.source_run_id) if order.source_run_id else None,
                lines=lines,
                materials=materials,
                operations=operations,
                reserve_structural_unit_ref1c=reserve_ref,
                product_structural_unit_ref1c=product_structural_unit_ref,
                planned_start_date=min(start_dates) if start_dates else None,
                planned_finish_date=max(finish_dates) if finish_dates else None,
                document_date=order.order_date,
            )
        )

    return entries, skipped, warnings


def _collect_close_entries(
    db: Session,
    generation_id: int,
    order_ids: List[int],
) -> Tuple[List[ProductionOrderCloseEntry], List[Dict[str, Any]]]:
    """
    Select candidate MRP orders for 1C close.
    Returns (entries, skipped) where skipped has diagnostic reasons to present in
    dry-run and fail-closed payloads.
    """
    entries: List[ProductionOrderCloseEntry] = []
    skipped: List[Dict[str, Any]] = []

    ids = [int(x) for x in order_ids if x is not None]
    if not ids:
        return entries, skipped

    rows = (
        db.query(ProductionOrder)
        .filter(ProductionOrder.order_id.in_(ids))
        .all()
    )
    found_ids = {int(order.order_id) for order in rows}
    for missing_id in [x for x in ids if x not in found_ids]:
        skipped.append({"order_id": missing_id, "reason": "ProductionOrder не найден"})

    for order in rows:
        if str(order.source or "1c").lower() != "mrp":
            skipped.append(
                {
                    "order_id": int(order.order_id),
                    "reason": f"source='{order.source}', закрываем только MRP-source",
                }
            )
            continue
        if bool(order.deletion_mark):
            skipped.append({"order_id": int(order.order_id), "reason": "deletion_mark=true"})
            continue
        order_ref1c = _clean_ref1c(order.order_ref1c)
        if not order_ref1c:
            skipped.append(
                {
                    "order_id": int(order.order_id),
                    "reason": "order_ref1c пустой, export in 1C отсутствует",
                }
            )
            continue
        link = _existing_link(db, int(order.order_id))
        if link is None:
            skipped.append(
                {
                    "order_id": int(order.order_id),
                    "reason": "SyncLink не найден для Document_ЗаказНаПроизводство",
                }
            )
            continue
        if str(link.status or "") != "success":
            skipped.append(
                {
                    "order_id": int(order.order_id),
                    "reason": f"SyncLink status='{link.status}', для закрытия нужен success",
                }
            )
            continue
        link_ref = _clean_ref1c(link.target_ref_key)
        if link_ref and link_ref != order_ref1c:
            skipped.append(
                {
                    "order_id": int(order.order_id),
                    "reason": "order_ref1c не совпадает с SyncLink.target_ref_key",
                }
            )
            continue
        if link.ledger_generation_id is not None and int(link.ledger_generation_id) != int(generation_id):
            skipped.append(
                {
                    "order_id": int(order.order_id),
                    "reason": "SyncLink принадлежит другой Ledger-цепи",
                }
            )
            continue

        entries.append(
            ProductionOrderCloseEntry(
                order_id=int(order.order_id),
                number=production_order_number(order),
                order_ref1c=order_ref1c,
                source_run_id=int(order.source_run_id) if order.source_run_id else None,
                ledger_generation_id=int(generation_id),
            )
        )
    return entries, skipped


def _persist_line_destinations(
    db: Session, entry: "ProductionOrderExportEntry"
) -> None:
    """Record on the order line where 1C was told the output will land.

    The Ledger counts an open production order as future supply only with an
    exact destination warehouse, and the 1C sync deliberately never writes
    PRODPLAN's own order lines back (it would overwrite quantity, spec and
    stage).  So the only honest place for that value is here — the step that
    decided it and sent it.

    Without it every launched order was rejected as evidence with "missing
    destination warehouse mapping": the requirement it was created for stayed
    uncovered, the item showed zero orders in production, and the same demand
    was offered for launch again.
    """
    for line in entry.lines:
        destination = _clean_ref1c(line.structural_unit_ref1c)
        if not destination or line.line_number is None:
            continue
        product = (
            db.query(ProductionProduct)
            .filter(
                ProductionProduct.order_id == int(entry.order_id),
                ProductionProduct.line_number == int(line.line_number),
            )
            .one_or_none()
        )
        if product is None:
            continue
        # This value is owned by the sanctioned 1C export.  A route can change
        # when an existing executor order is recalculated (for example, weld
        # output must move from assembly to the following paint workshop), so
        # preserving a non-empty historical destination would keep future
        # supply attached to the wrong warehouse.
        product.destination_warehouse_ref1c = destination


def _export_line_token(entry: ProductionOrderExportEntry, kind: str, axes: Dict[str, Any]) -> int:
    """Versioned deterministic positive Int64 for 1C ``КлючСвязи``."""
    if (
        entry.ledger_generation_id is None or entry.source_run_id is None
        or entry.freeze_version is None
    ):
        raise MrpMutationLineageError("production export has no accepted generation/run/freeze")
    def norm(value: Any) -> Any:
        if isinstance(value, Decimal):
            return format(value.normalize(), "f") if value else "0"
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, float):
            return format(Decimal(str(value)).normalize(), "f") if value else "0"
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        return value
    payload = {
        "v": 1, "kind": kind, "order": entry.order_id,
        "generation": entry.ledger_generation_id, "run": entry.source_run_id,
        "freeze": entry.freeze_version, "axes": axes,
    }
    canonical = json.dumps(norm(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value = int.from_bytes(hashlib.sha256(canonical.encode("utf-8")).digest()[:8], "big") & ((1 << 63) - 1)
    return value or 1


def _build_header_payload(
    entry: ProductionOrderExportEntry,
    defaults: Optional[ProductionOrderExportDefaults] = None,
) -> Dict[str, Any]:
    defaults = defaults or ProductionOrderExportDefaults()
    comment = (
        f"PRODPLAN source=production_order/{entry.order_id}; "
        f"run={entry.source_run_id or 0}; number={entry.number}"
    )
    products = []
    for ln in entry.lines:
        product_link_key = _export_line_token(entry, "product", {
            "item": ln.item_ref1c, "characteristic": ln.characteristic_ref1c or EMPTY_REF1C,
            "unit": ln.unit_ref1c or "", "qty": ln.qty, "spec": ln.spec_ref1c or "",
            "structural_unit": ln.structural_unit_ref1c or "",
        })
        row: Dict[str, Any] = {
            "LineNumber": int(ln.line_number or len(products) + 1),
            "Номенклатура_Key": ln.item_ref1c,
            "Количество": float(ln.qty),
            "КлючСвязи": product_link_key,
            **(
                {"СтруктурнаяЕдиница_Key": ln.structural_unit_ref1c or defaults.product_structural_unit_ref1c}
                if (ln.structural_unit_ref1c or defaults.product_structural_unit_ref1c)
                else {}
            ),
            **(
                {"Характеристика_Key": ln.characteristic_ref1c}
                if ln.characteristic_ref1c
                else {}
            ),
        }
        if ln.spec_ref1c:
            row["Спецификация_Key"] = ln.spec_ref1c
        _add_unit_payload(row, ln.unit_ref1c)
        products.append(row)
    stock_lines = []
    for idx, ln in enumerate(entry.materials, start=1):
        row: Dict[str, Any] = {
            "LineNumber": idx,
            "Номенклатура_Key": ln.item_ref1c,
            "Количество": float(ln.qty),
        }
        _add_unit_payload(row, ln.unit_ref1c)
        if ln.spec_ref1c:
            row["Спецификация_Key"] = ln.spec_ref1c
        if ln.reserve_structural_unit_ref1c:
            row["СтруктурнаяЕдиница_Key"] = ln.reserve_structural_unit_ref1c
        row["КлючСвязи"] = _export_line_token(entry, "material", {
            "item": ln.item_ref1c, "unit": ln.unit_ref1c or "", "qty": ln.qty,
            "spec": ln.spec_ref1c or "", "reserve": ln.reserve_structural_unit_ref1c or "",
        })
        stock_lines.append(row)
    operation_lines = []
    for idx, op in enumerate(entry.operations, start=1):
        product_token = _export_line_token(entry, "product", {
            "item": next((p.item_ref1c for p in entry.lines if int(p.line_number) == int(op.product_link_key or 0)), ""),
            "characteristic": next((p.characteristic_ref1c or EMPTY_REF1C for p in entry.lines if int(p.line_number) == int(op.product_link_key or 0)), EMPTY_REF1C),
            "unit": next((p.unit_ref1c or "" for p in entry.lines if int(p.line_number) == int(op.product_link_key or 0)), ""),
            "qty": next((p.qty for p in entry.lines if int(p.line_number) == int(op.product_link_key or 0)), 0),
            "spec": next((p.spec_ref1c or "" for p in entry.lines if int(p.line_number) == int(op.product_link_key or 0)), ""),
            "structural_unit": next((p.structural_unit_ref1c or "" for p in entry.lines if int(p.line_number) == int(op.product_link_key or 0)), ""),
        })
        row: Dict[str, Any] = {
            "LineNumber": idx,
            "Операция_Key": op.operation_ref1c,
            "КоличествоПлан": float(op.qty),
            "НормаВремени": float(op.time_norm),
            "Нормочасы": float(op.norm_hours),
            "КлючСвязи": _export_line_token(entry, "operation", {
                "operation": op.operation_ref1c, "unit": op.unit_ref1c or "", "qty": op.qty,
                "time_norm": op.time_norm, "norm_hours": op.norm_hours,
                "structural_unit": op.structural_unit_ref1c or "", "product": product_token,
            }),
        }
        if op.product_link_key:
            row["КлючСвязиПродукция"] = product_token
        _add_unit_payload(row, op.unit_ref1c)
        if op.structural_unit_ref1c:
            row["СтруктурнаяЕдиница_Key"] = op.structural_unit_ref1c
        elif defaults.operation_structural_unit_ref1c:
            row["СтруктурнаяЕдиница_Key"] = defaults.operation_structural_unit_ref1c
        operation_lines.append(row)
    if entry.document_date is None:
        raise MrpMutationLineageError(
            "production order has no durable document date; export is blocked"
        )
    document_dt = _fmt_1c_datetime(entry.document_date)
    if not document_dt:
        raise MrpMutationLineageError(
            "production order has no valid durable document date; export is blocked"
        )
    payload: Dict[str, Any] = {
        "Number": entry.number,
        "Date": document_dt,
        "Posted": False,
        "Комментарий": comment,
        "Продукция": products,
    }
    # Planned dates are date-only in the canonical model.  Derive their time
    # component from the durable document timestamp so an identical retry
    # produces the identical payload hash.
    planned_time_source = document_dt
    start_dt = _combine_planned_date_with_time(entry.planned_start_date, planned_time_source)
    finish_dt = _combine_planned_date_with_time(entry.planned_finish_date or entry.planned_start_date, planned_time_source)
    if start_dt:
        payload["Старт"] = start_dt
    if finish_dt:
        payload["Финиш"] = finish_dt
    if stock_lines:
        payload["Запасы"] = stock_lines
    if operation_lines:
        payload["Операции"] = operation_lines
        payload["ЗапланированыОперации"] = True
    if defaults.organization_ref1c:
        payload["Организация_Key"] = defaults.organization_ref1c
    if defaults.structural_unit_ref1c:
        payload["СтруктурнаяЕдиница_Key"] = defaults.structural_unit_ref1c
    product_structural_unit = entry.product_structural_unit_ref1c or defaults.product_structural_unit_ref1c
    if product_structural_unit:
        payload["СтруктурнаяЕдиницаПродукции_Key"] = product_structural_unit
    if defaults.operation_structural_unit_ref1c:
        payload["СтруктурнаяЕдиницаОпераций_Key"] = defaults.operation_structural_unit_ref1c
    payload["СтруктурнаяЕдиницаРезерв_Key"] = entry.reserve_structural_unit_ref1c or EMPTY_REF1C
    if entry.origin_token:
        payload["Комментарий"] = _add_origin_marker(payload["Комментарий"], entry.origin_token)
    for table_name in ("Продукция", "Запасы", "Операции"):
        tokens = [row.get("КлючСвязи") for row in payload.get(table_name, [])]
        if any(not isinstance(token, int) or token <= 0 or token >= 2**63 for token in tokens):
            raise MrpMutationLineageError(f"invalid 1C КлючСвязи in {table_name}")
        if len(tokens) != len(set(tokens)):
            raise MrpMutationLineageError(f"1C КлючСвязи collision in {table_name}")
    return payload


def _build_close_payload(
    entry: ProductionOrderCloseEntry | ProductionOrderExportEntry,
    close_defaults: ProductionOrderCloseDefaults,
) -> Dict[str, Any]:
    state_ref = _clean_ref1c(close_defaults.close_state_ref1c)
    if not state_ref:
        raise MrpMutationLineageError(
            "default_production_order_done_state_ref1c is not set in OData config"
        )
    close_variant_ref = _clean_ref1c(close_defaults.close_variant_ref1c)
    if not close_variant_ref:
        raise MrpMutationLineageError(
            "default_production_order_done_variant_ref1c is not set in OData config"
        )
    payload: Dict[str, Any] = {
        "СостояниеЗаказа_Key": state_ref,
        "ВариантЗавершения": close_variant_ref,
    }
    return payload


def close_production_orders_to_1c(
    db: Session,
    order_ids: List[int],
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Close the given MRP production orders in 1C as Document_ЗаказНаПроизводство.

    Default is `dry_run=True`; pass `dry_run=False` for actual PATCH.
    Fail-closed: all lineage and link checks must pass before any remote I/O.
    """
    selected_ids = sorted({int(order_id) for order_id in order_ids})
    selected_orders = (
        db.query(ProductionOrder)
        .filter(ProductionOrder.order_id.in_(selected_ids))
        .all()
    )
    if {int(order.order_id) for order in selected_orders} != set(selected_ids):
        raise MrpMutationLineageError("one or more selected production orders do not exist")
    generation_id = require_materialized_orders(
        db, selected_orders, consumer="one_c_production_order_close"
    )
    if not selected_orders:
        return {
            "status": "ok",
            "dry_run": True,
            "entity": PRODUCTION_ORDER_ENTITY,
            "orders_requested": 0,
            "orders_eligible": 0,
            "orders_closed": 0,
            "orders_error": 0,
            "skipped_rows": [],
            "entries": [],
        }

    run_id = int(selected_orders[0].source_run_id) if selected_orders else None
    run = db.get(PlanningRun, run_id) if run_id else None
    if run is None or run.active_freeze_version is None:
        raise MrpMutationLineageError("production close has no active planning freeze")

    entries, skipped = _collect_close_entries(db, generation_id=generation_id, order_ids=selected_ids)

    for entry in entries:
        entry.freeze_version = int(run.active_freeze_version)

    config = _load_odata_config()
    close_defaults = _close_defaults(config)

    payloads_by_order: Dict[int, Dict[str, Any]] = {}
    for entry in entries:
        payload = _build_close_payload(entry, close_defaults)
        payloads_by_order[int(entry.order_id)] = {
            "order_id": entry.order_id,
            "payload": payload,
        }

    summary: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "entity": PRODUCTION_ORDER_ENTITY,
        "orders_requested": len(order_ids),
        "orders_eligible": len(entries),
        "orders_closed": 0,
        "orders_error": 0,
        "skipped_rows": skipped,
        "payloads": list(payloads_by_order.values()),
        "entries": [],
    }

    if dry_run:
        summary["entries"] = [asdict(entry) for entry in entries]
        return summary

    if not entries:
        summary["entries"] = []
        return summary

    client = _create_odata_client(config, OData1CClient)
    closed = 0
    errored = 0
    for entry in entries:
        payload_envelope = payloads_by_order.get(int(entry.order_id), {})
        payload = payload_envelope.get("payload", {})
        try:
            order_ref1c = _clean_ref1c(entry.order_ref1c)
            if not order_ref1c:
                raise ValueError("order_ref1c is missing")

            link = _existing_link(db, int(entry.order_id))
            if link is None or str(link.status or "") != "success":
                raise ValueError("SyncLink lost or no longer successful before close write")
            link_ref = _clean_ref1c(link.target_ref_key)
            if link_ref and link_ref != order_ref1c:
                raise ValueError("SyncLink target_ref_key diverged before close write")

            client.patch(f"{PRODUCTION_ORDER_ENTITY}(guid'{order_ref1c}')", payload)
            closed += 1
        except Exception as exc:
            entry.status = "error"
            entry.error = str(exc)
            errored += 1

    summary["orders_closed"] = closed
    summary["orders_error"] = errored
    summary["status"] = "ok" if errored == 0 else "partial_error"
    summary["entries"] = [asdict(entry) for entry in entries]
    return summary


def _entry_origin_token(db: Session, entry: ProductionOrderExportEntry) -> str:
    products = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.order_id == int(entry.order_id))
        .order_by(ProductionProduct.line_number.asc(), ProductionProduct.product_id.asc())
        .all()
    )
    durable_sources = [
        str(p.source_mrp_allocation_key)
        for p in products
        if str(p.source_mrp_allocation_key or "").strip()
    ]
    identity = {
        # Allocation keys are the preferred durable demand identity. Never put
        # run/order ids in the marker: they diverge between instances.
        "allocations": sorted(durable_sources),
        "lines": [
            {
                "item_ref1c": line.item_ref1c,
                "characteristic_ref1c": line.characteristic_ref1c or "",
                "qty": float(line.qty),
            }
            for line in entry.lines
        ],
        "planned_start": str(entry.planned_start_date or ""),
        "planned_finish": str(entry.planned_finish_date or ""),
    }
    return _origin_token("production_order", identity)


def _upsert_link(
    db: Session,
    *,
    entry: ProductionOrderExportEntry,
    payload_hash: str,
    target_ref_key: Optional[str],
    status: str,
    last_error: Optional[str],
) -> None:
    existing_before = _existing_link(db, int(entry.order_id))
    if (
        existing_before is not None
        and existing_before.ledger_generation_id is not None
        and entry.ledger_generation_id is not None
        and int(existing_before.ledger_generation_id) != int(entry.ledger_generation_id)
    ):
        verified_reuse = (
            bool(entry.unpost_before_patch)
            and str(existing_before.status or "") == "success"
            and _clean_ref1c(existing_before.target_ref_key)
            == _clean_ref1c(target_ref_key)
        )
        if not verified_reuse:
            raise RuntimeError("production SyncLink belongs to another Ledger generation")
    _upsert_sync_link(
        db,
        SyncLink,
        source_doctype="production_order",
        source_id=int(entry.order_id),
        target_entity=PRODUCTION_ORDER_ENTITY,
        target_number=entry.number,
        payload_hash=payload_hash,
        target_ref_key=target_ref_key,
        status=status,
        last_error=last_error,
    )
    db.flush()
    link = _existing_link(db, int(entry.order_id))
    if link is None:
        raise RuntimeError("production SyncLink upsert was not persisted")
    if entry.ledger_generation_id is None:
        raise MrpMutationLineageError("production SyncLink has no accepted Ledger generation")
    link.ledger_generation_id = int(entry.ledger_generation_id)


def _validate_existing_retry_link(
    *,
    entry: ProductionOrderExportEntry,
    link: Optional[SyncLink],
    expected_payload_hash: str,
    order_ref1c: str,
) -> Optional[str]:
    """Return a verified 1C ref or fail before any client/DB mutation.

    A legacy ``order_ref1c`` is not proof of what was sent to 1C.  Likewise a
    SyncLink without the accepted generation or with another canonical payload
    cannot safely be retried.  This deliberately refuses the tempting
    "probably the same order" recovery path: the origin-marker recovery below
    is the only network lookup allowed after this local proof succeeds.
    """
    if link is None:
        if order_ref1c:
            raise MrpMutationLineageError(
                "production_orders.order_ref1c has no verifiable SyncLink; "
                "legacy retry is blocked"
            )
        return None

    link_ref = _clean_ref1c(link.target_ref_key)
    # A link without a target ref is merely a prior failed attempt.  It cannot
    # be accepted as an existing document, but its payload/generation still
    # must agree before we can create or recover anything.
    if entry.ledger_generation_id is None:
        raise MrpMutationLineageError("production export has no accepted Ledger generation")
    durable_success = (
        str(link.status or "") == "success" and link_ref and order_ref1c == link_ref
    )
    if link.ledger_generation_id is None:
        # Canonical rebuilds preserve successful external identity while
        # clearing rebuildable Ledger lineage.  The agreeing source Ref_Key is
        # sufficient for the creation command to reuse that exact document.
        if durable_success:
            return link_ref
        raise MrpMutationLineageError("production SyncLink has no accepted Ledger generation")
    same_generation = int(link.ledger_generation_id) == int(entry.ledger_generation_id)
    if not same_generation and not durable_success:
        raise MrpMutationLineageError("production SyncLink belongs to another Ledger generation")
    # A successfully created 1C order is a durable executor document.  A later
    # accepted physical Ledger generation may materialize the same live MRP
    # obligation with a different current payload, but that must not create a
    # second 1C order.  The verified document itself may be unposted, patched
    # and reposted by an explicit retry. Inside the same immutable generation,
    # a payload mismatch still means local lineage corruption and fails closed.
    if same_generation and (
        not link.payload_hash or str(link.payload_hash) != expected_payload_hash
    ):
        raise MrpMutationLineageError("production SyncLink payload does not match canonical export payload")
    if order_ref1c and order_ref1c != link_ref:
        raise MrpMutationLineageError("production_orders.order_ref1c disagrees with verified SyncLink")
    return link_ref or None


def export_production_orders_to_1c(
    db: Session,
    order_ids: List[int],
    *,
    dry_run: bool = True,
    comment_suffixes: Optional[Dict[int, str]] = None,
    basis_order_refs: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """
    Export the given internal MRP production_orders to 1C as
    Document_ЗаказНаПроизводство with Posted=false, then operationally posts
    each created 1C document.

    Default is `dry_run=True` per plan safety rule: the caller must pass
    `dry_run=False` to actually write into the configured 1C base.
    """
    selected_ids = sorted({int(order_id) for order_id in order_ids})
    selected_orders = (
        db.query(ProductionOrder)
        .options(joinedload(ProductionOrder.products))
        .filter(ProductionOrder.order_id.in_(selected_ids))
        .all()
    )
    if {int(order.order_id) for order in selected_orders} != set(selected_ids):
        raise MrpMutationLineageError(
            "one or more selected production orders do not exist"
        )
    generation_id = require_materialized_orders(
        db, selected_orders, consumer="one_c_production_order_export"
    )
    run_id = int(selected_orders[0].source_run_id) if selected_orders else None
    run = db.get(PlanningRun, run_id) if run_id else None
    if run is None or run.active_freeze_version is None:
        raise MrpMutationLineageError("production export has no active planning freeze")
    entries, skipped, warnings = _collect_export_entries(db, list(order_ids))
    for entry in entries:
        entry.ledger_generation_id = int(generation_id)
        entry.freeze_version = int(run.active_freeze_version)

    config = _load_odata_config()
    defaults = _export_defaults(config)

    # Build every canonical payload *before* accepting any local success/no-op.
    # A SyncLink is valid only for this exact immutable ledger generation and
    # this exact full payload, including deterministic 1C line tokens.
    # `comment_suffixes` lets a caller annotate a specific order's Комментарий
    # without cloning the payload builder. `basis_order_refs` (order_id ->
    # Ref_Key заказа-основания) sets the native 1С basis fields on the payload:
    # Document_ЗаказНаПроизводство carries a dedicated typed reference
    # ЗаказНаПроизводствоОснование_Key plus the generic composite
    # ДокументОснование/_Type. Used by the paint→weld chain to open the
    # сварка-заказ «на основании» окраска-заказа. Default None for both =>
    # byte-for-byte prior behaviour.
    suffixes = comment_suffixes or {}
    basis_refs = basis_order_refs or {}
    payloads_by_order: Dict[int, Dict[str, Any]] = {}
    for entry in entries:
        entry.origin_token = _entry_origin_token(db, entry)
        payload = _build_header_payload(entry, defaults)
        suffix = suffixes.get(int(entry.order_id))
        if suffix:
            payload["Комментарий"] = f"{payload['Комментарий']}; {suffix}"
        basis_ref = _clean_ref1c(basis_refs.get(int(entry.order_id)))
        if basis_ref:
            payload["ЗаказНаПроизводствоОснование_Key"] = basis_ref
            payload["ДокументОснование"] = basis_ref
            payload["ДокументОснование_Type"] = f"StandardODATA.{PRODUCTION_ORDER_ENTITY}"
        payloads_by_order[int(entry.order_id)] = {
            "order_id": entry.order_id,
            "number": entry.number,
            "payload": payload,
        }

    # Pre-flight: no 1C client and no writes until every existing reference is
    # proven to belong to the same accepted generation and canonical payload.
    eligible: List[ProductionOrderExportEntry] = []
    already_linked: List[ProductionOrderExportEntry] = []
    for entry in entries:
        envelope = payloads_by_order[int(entry.order_id)]
        expected_hash = _payload_hash(envelope["payload"])
        link = _existing_link(db, entry.order_id)
        order_row = db.query(ProductionOrder).filter(ProductionOrder.order_id == entry.order_id).one()
        verified_ref = _validate_existing_retry_link(
            entry=entry,
            link=link,
            expected_payload_hash=expected_hash,
            order_ref1c=_clean_ref1c(order_row.order_ref1c),
        )
        same_link_generation = (
            link is not None
            and link.ledger_generation_id is not None
            and int(link.ledger_generation_id) == int(entry.ledger_generation_id)
        )
        if link and link.status == "success" and verified_ref and same_link_generation:
            entry.status = "existing"
            entry.target_ref_key = verified_ref
            entry.reason = "уже выгружен в 1С (проверенный sync_link)"
            already_linked.append(entry)
            continue
        if verified_ref:
            entry.target_ref_key = verified_ref
            entry.unpost_before_patch = True
            entry.reason = (
                "повторная отправка: проверенный 1С-документ будет снят с "
                "проведения, обновлён и проведён повторно"
            )
        eligible.append(entry)

    payloads = [payloads_by_order[int(entry.order_id)] for entry in eligible]
    summary: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "entity": PRODUCTION_ORDER_ENTITY,
        "orders_requested": len(order_ids),
        "orders_eligible": len(eligible),
        "orders_already_linked": len(already_linked),
        "orders_created": 0,
        "orders_error": 0,
        "skipped_rows": skipped,
        "warnings": warnings,
        "entries": [],
    }

    if dry_run:
        summary["entries"] = [asdict(e) for e in entries]
        summary["payloads"] = payloads
        return summary

    # ----- real write below -----
    client = _create_odata_client(config, OData1CClient)

    # Recover a POST performed by another PRODPLAN instance before creating
    # anything new.  This intentionally happens after payload construction so
    # every eligible entry carries its deterministic marker.
    recovered: List[ProductionOrderExportEntry] = []
    pending_payloads: List[Dict[str, Any]] = []
    pending_entries: List[ProductionOrderExportEntry] = []
    for entry, envelope in zip(eligible, payloads):
        doc = _find_document_by_origin(
            client,
            entity=PRODUCTION_ORDER_ENTITY,
            token=str(entry.origin_token),
        )
        ref_key = _clean_ref1c((doc or {}).get("Ref_Key"))
        if not ref_key:
            pending_entries.append(entry)
            pending_payloads.append(envelope)
            continue
        entry.status = "existing"
        entry.target_ref_key = ref_key
        entry.reason = "найден существующий документ 1С по prodplan-origin"
        _upsert_link(
            db,
            entry=entry,
            payload_hash=_payload_hash(envelope["payload"]),
            target_ref_key=ref_key,
            status="success",
            last_error=None,
        )
        order_row = db.query(ProductionOrder).filter(
            ProductionOrder.order_id == entry.order_id
        ).one()
        order_row.order_ref1c = ref_key
        _persist_line_destinations(db, entry)
        recovered.append(entry)
    if recovered:
        db.commit()

    def _mark_success(entry: ProductionOrderExportEntry, ref_key: str) -> None:
        _post_document_operational(
            client,
            entity=PRODUCTION_ORDER_ENTITY,
            ref_key=ref_key,
            unpost_first=False,
        )
        # Stamp success on production_orders.order_ref1c so the journal stops
        # treating it as MRP-only.
        order_row = db.query(ProductionOrder).filter(ProductionOrder.order_id == entry.order_id).one()
        order_row.order_ref1c = ref_key
        _persist_line_destinations(db, entry)

    created, errored = _post_export_entries(
        db,
        entries=zip(pending_entries, pending_payloads),
        client=client,
        target_entity=PRODUCTION_ORDER_ENTITY,
        missing_ref_error=f"1C did not return Ref_Key for the new {PRODUCTION_ORDER_ENTITY}",
        upsert_link=lambda **kwargs: _upsert_link(db, **kwargs),
        on_success=_mark_success,
        log_error=lambda entry: f"[1C production export] order_id={entry.order_id} failed: {entry.error}",
    )

    summary["orders_created"] = created
    summary["orders_recovered"] = len(recovered)
    summary["orders_error"] = errored
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored == 0 else "partial_error"
    return summary
