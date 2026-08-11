"""Single source of truth for "which workshop makes this part".

Business rule: a part is routed to a workshop ONLY through its
specification's production kind — Specification.production_kind_id →
ResourceProductionKind.resource_id. The kind→workshop binding is maintained
in PRODPLAN on the "Ресурсы" page (one kind → one workshop). An explicit
manual assignment (ProductionOrderLineState.workshop_id) always wins over
the automatic resolution.

The legacy fallback — deriving the workshop from the spec's dominant stage
via ResourceStage — is deliberately NOT part of resolution. It used to mask
specs with an unfilled production kind. The stage chain survives only as a
RECOMMENDATION (see suggest_workshops_by_stage) for the manual-review page.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    ProductionKind,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    ResourceProductionKind,
    ResourceStage,
    SpecComponent,
    SpecOperation,
    Specification,
    WorkshopWarehouseBinding,
)
from .bom_specification_resolver import BomSpecificationResolver
from .production_control_common import to_float as _to_float

REASON_OK = "OK"
REASON_NO_SPEC = "NO_SPEC"
REASON_NO_PRODUCTION_KIND = "NO_PRODUCTION_KIND"
REASON_KIND_NOT_BOUND = "KIND_NOT_BOUND"
REASON_NO_WAREHOUSE_BINDING = "NO_WAREHOUSE_BINDING"

PROBLEM_REASON_CODES = (
    REASON_NO_SPEC,
    REASON_NO_PRODUCTION_KIND,
    REASON_KIND_NOT_BOUND,
    REASON_NO_WAREHOUSE_BINDING,
)


@dataclass
class WorkshopDiagnosis:
    status: str  # "ok" | "problem"
    reason_code: str
    reason_text: str
    recommendation: str
    workshop_id: Optional[int] = None
    workshop_source: Optional[str] = None  # "state" | "production_kind" | None
    spec_id: Optional[int] = None
    spec_name: Optional[str] = None
    production_kind_id: Optional[int] = None
    production_kind_name: Optional[str] = None
    suggested_resource_id: Optional[int] = None
    suggested_resource_name: Optional[str] = None
    suggested_stage_id: Optional[int] = None
    suggested_stage_name: Optional[str] = None


def reason_text_for(code: str, *, spec_name: str = "", kind_name: str = "", workshop_name: str = "") -> str:
    if code == REASON_NO_SPEC:
        return "У детали нет спецификации по умолчанию"
    if code == REASON_NO_PRODUCTION_KIND:
        return f"В спецификации «{spec_name or '—'}» не заполнен вид производства"
    if code == REASON_KIND_NOT_BOUND:
        return f"Вид производства «{kind_name or '—'}» не привязан ни к одному участку"
    if code == REASON_NO_WAREHOUSE_BINDING:
        return f"Для участка «{workshop_name or '—'}» не настроена привязка склада"
    return "Участок определён"


def recommendation_for(code: str, *, suggested_resource_name: str = "") -> str:
    if code == REASON_NO_SPEC:
        return "Назначьте детали основную спецификацию в 1С и выполните синхронизацию спецификаций"
    if code == REASON_NO_PRODUCTION_KIND:
        return "Заполните реквизит «Вид производства» в спецификации в 1С и выполните синхронизацию спецификаций"
    if code == REASON_KIND_NOT_BOUND:
        base = "Привяжите вид производства к участку на странице «Ресурсы»"
        if suggested_resource_name:
            return f"{base}. По этапам спецификации подходит участок «{suggested_resource_name}»"
        return base
    if code == REASON_NO_WAREHOUSE_BINDING:
        return (
            "Настройте склад участка в настройках производственного контроля — "
            "без него не создаются перемещения материалов"
        )
    return ""


# ---------------------------------------------------------------------------
# Resolution (no stage fallback)
# ---------------------------------------------------------------------------


def resolve_workshop_for_specs(db: Session, spec_ids: Sequence[int]) -> Dict[int, int]:
    """Batch: spec_id -> resource_id via the spec's production kind."""
    ids = sorted({int(spec_id) for spec_id in spec_ids if spec_id})
    if not ids:
        return {}
    result: Dict[int, int] = {}
    for row in (
        db.query(Specification.spec_id, ResourceProductionKind.resource_id)
        .join(
            ResourceProductionKind,
            ResourceProductionKind.production_kind_id == Specification.production_kind_id,
        )
        .filter(Specification.spec_id.in_(ids))
        .order_by(ResourceProductionKind.id.asc())
        .all()
    ):
        result.setdefault(int(row.spec_id), int(row.resource_id))
    return result


def resolve_workshop_for_spec(db: Session, spec_id: Optional[int]) -> Optional[int]:
    if not spec_id:
        return None
    return resolve_workshop_for_specs(db, [int(spec_id)]).get(int(spec_id))


def default_spec_ids_for_items(db: Session, item_ids: Sequence[int]) -> Dict[int, int]:
    ids = sorted({int(item_id) for item_id in item_ids if item_id})
    if not ids:
        return {}
    resolver = BomSpecificationResolver(db)
    return {
        item_id: int(spec_id)
        for item_id in ids
        if (spec_id := resolver.default_spec_id(item_id)) is not None
    }


def spec_id_for_product(db: Session, product: ProductionProduct) -> Optional[int]:
    if product.spec_id:
        return int(product.spec_id)
    return default_spec_ids_for_items(db, [int(product.item_id)]).get(int(product.item_id))


def resolve_workshop_for_product(
    db: Session,
    product: ProductionProduct,
    spec_id: Optional[int] = None,
) -> Optional[int]:
    """Explicit line assignment wins; otherwise spec → production kind → workshop."""
    state = (
        db.query(ProductionOrderLineState.workshop_id, ProductionOrderLineState.workshop_id_source)
        .filter(ProductionOrderLineState.product_id == int(product.product_id))
        .first()
    )
    if state and state[0] and str(state[1] or "") not in {"auto", "legacy"}:
        return int(state[0])
    resolved_spec = int(spec_id) if spec_id else spec_id_for_product(db, product)
    return resolve_workshop_for_spec(db, resolved_spec)


def warehouse_binding_for_workshop(
    db: Session, workshop_id: Optional[int]
) -> Optional[WorkshopWarehouseBinding]:
    if not workshop_id:
        return None
    return (
        db.query(WorkshopWarehouseBinding)
        .filter(WorkshopWarehouseBinding.workshop_id == int(workshop_id))
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# Stage chain: display and recommendations only
# ---------------------------------------------------------------------------


def main_stages_for_specs(
    db: Session, spec_ids: Sequence[int]
) -> Dict[int, Tuple[Optional[int], Optional[str]]]:
    """spec_id -> (stage_id, stage_name): the spec's dominant stage by hours,
    falling back to the first component stage. Used for the journal's stage
    column and as the input for stage-based suggestions."""
    ids = sorted({int(spec_id) for spec_id in spec_ids if spec_id})
    if not ids:
        return {}

    stage_by_spec: Dict[int, int] = {}
    best_hours: Dict[int, float] = {}
    for row in (
        db.query(
            SpecOperation.spec_id,
            SpecOperation.stage_id,
            func.sum(SpecOperation.time_norm).label("hours"),
        )
        .filter(SpecOperation.spec_id.in_(ids), SpecOperation.stage_id.isnot(None))
        .group_by(SpecOperation.spec_id, SpecOperation.stage_id)
        .all()
    ):
        spec_id = int(row.spec_id)
        hours = _to_float(row.hours)
        if spec_id not in best_hours or hours > best_hours[spec_id]:
            best_hours[spec_id] = hours
            stage_by_spec[spec_id] = int(row.stage_id)

    missing_ids = [spec_id for spec_id in ids if spec_id not in stage_by_spec]
    if missing_ids:
        for row in (
            db.query(SpecComponent.spec_id, SpecComponent.stage_id)
            .filter(SpecComponent.spec_id.in_(missing_ids), SpecComponent.stage_id.isnot(None))
            .order_by(SpecComponent.component_id.asc())
            .all()
        ):
            stage_by_spec.setdefault(int(row.spec_id), int(row.stage_id))

    stage_ids = sorted(set(stage_by_spec.values()))
    stage_name_by_id: Dict[int, str] = {}
    if stage_ids:
        for row in (
            db.query(ProductionStage.stage_id, ProductionStage.stage_name)
            .filter(ProductionStage.stage_id.in_(stage_ids))
            .all()
        ):
            stage_name_by_id[int(row.stage_id)] = str(row.stage_name or "")

    return {
        spec_id: (stage_id, stage_name_by_id.get(stage_id))
        for spec_id, stage_id in stage_by_spec.items()
    }


def suggest_workshops_by_stage(
    db: Session, spec_ids: Sequence[int]
) -> Dict[int, Tuple[int, str, int, str]]:
    """spec_id -> (resource_id, resource_name, stage_id, stage_name) via the
    legacy stage chain. RECOMMENDATION ONLY — never used for routing."""
    stages = main_stages_for_specs(db, spec_ids)
    stage_ids = sorted({stage_id for stage_id, _ in stages.values() if stage_id})
    if not stage_ids:
        return {}
    resource_by_stage: Dict[int, Tuple[int, str]] = {}
    for row in (
        db.query(ResourceStage.stage_id, ResourceStage.resource_id, ProductionResource.resource_name)
        .join(ProductionResource, ProductionResource.resource_id == ResourceStage.resource_id)
        .filter(ResourceStage.stage_id.in_(stage_ids))
        .order_by(ResourceStage.id.asc())
        .all()
    ):
        resource_by_stage.setdefault(
            int(row.stage_id), (int(row.resource_id), str(row.resource_name or ""))
        )
    result: Dict[int, Tuple[int, str, int, str]] = {}
    for spec_id, (stage_id, stage_name) in stages.items():
        if not stage_id or stage_id not in resource_by_stage:
            continue
        resource_id, resource_name = resource_by_stage[stage_id]
        result[spec_id] = (resource_id, resource_name, stage_id, stage_name or "")
    return result


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def diagnose_specs(db: Session, spec_ids: Sequence[int]) -> Dict[int, WorkshopDiagnosis]:
    """Batch diagnosis of the kind→workshop→warehouse chain, without line
    state (catalog view). Keys are spec_ids; missing specs are not reported
    here — the caller handles NO_SPEC for items without a spec."""
    ids = sorted({int(spec_id) for spec_id in spec_ids if spec_id})
    if not ids:
        return {}

    rows = (
        db.query(
            Specification.spec_id,
            Specification.spec_name,
            Specification.production_kind_id,
            ProductionKind.name.label("kind_name"),
            ResourceProductionKind.resource_id,
            ProductionResource.resource_name,
            WorkshopWarehouseBinding.binding_id,
        )
        .outerjoin(ProductionKind, ProductionKind.id == Specification.production_kind_id)
        .outerjoin(
            ResourceProductionKind,
            ResourceProductionKind.production_kind_id == Specification.production_kind_id,
        )
        .outerjoin(
            ProductionResource,
            ProductionResource.resource_id == ResourceProductionKind.resource_id,
        )
        .outerjoin(
            WorkshopWarehouseBinding,
            WorkshopWarehouseBinding.workshop_id == ResourceProductionKind.resource_id,
        )
        .filter(Specification.spec_id.in_(ids))
        .order_by(ResourceProductionKind.id.asc())
        .all()
    )

    suggestions = suggest_workshops_by_stage(db, ids)

    result: Dict[int, WorkshopDiagnosis] = {}
    for row in rows:
        spec_id = int(row.spec_id)
        if spec_id in result:
            continue
        spec_name = str(row.spec_name or "")
        kind_name = str(row.kind_name or "")
        suggested = suggestions.get(spec_id)
        if not row.production_kind_id:
            code = REASON_NO_PRODUCTION_KIND
            workshop_id = None
        elif not row.resource_id:
            code = REASON_KIND_NOT_BOUND
            workshop_id = None
        elif not row.binding_id:
            code = REASON_NO_WAREHOUSE_BINDING
            workshop_id = int(row.resource_id)
        else:
            code = REASON_OK
            workshop_id = int(row.resource_id)
        result[spec_id] = WorkshopDiagnosis(
            status="ok" if code == REASON_OK else "problem",
            reason_code=code,
            reason_text=reason_text_for(
                code,
                spec_name=spec_name,
                kind_name=kind_name,
                workshop_name=str(row.resource_name or ""),
            ),
            recommendation=recommendation_for(
                code, suggested_resource_name=suggested[1] if suggested else ""
            ),
            workshop_id=workshop_id,
            workshop_source="production_kind" if workshop_id else None,
            spec_id=spec_id,
            spec_name=spec_name,
            production_kind_id=int(row.production_kind_id) if row.production_kind_id else None,
            production_kind_name=kind_name or None,
            suggested_resource_id=suggested[0] if suggested else None,
            suggested_resource_name=suggested[1] if suggested else None,
            suggested_stage_id=suggested[2] if suggested else None,
            suggested_stage_name=suggested[3] if suggested else None,
        )
    return result


def no_spec_diagnosis() -> WorkshopDiagnosis:
    return WorkshopDiagnosis(
        status="problem",
        reason_code=REASON_NO_SPEC,
        reason_text=reason_text_for(REASON_NO_SPEC),
        recommendation=recommendation_for(REASON_NO_SPEC),
    )


def diagnose_product(db: Session, product: ProductionProduct) -> WorkshopDiagnosis:
    """Diagnosis for one journal line. A manual state.workshop_id satisfies
    routing; only the warehouse binding remains to be checked then."""
    spec_id = spec_id_for_product(db, product)

    state = (
        db.query(ProductionOrderLineState.workshop_id, ProductionOrderLineState.workshop_id_source)
        .filter(ProductionOrderLineState.product_id == int(product.product_id))
        .first()
    )
    manual_workshop = int(state[0]) if state and state[0] and str(state[1] or "") not in {"auto", "legacy"} else None
    if manual_workshop:
        binding = warehouse_binding_for_workshop(db, manual_workshop)
        workshop_name = ""
        resource = (
            db.query(ProductionResource.resource_name)
            .filter(ProductionResource.resource_id == manual_workshop)
            .first()
        )
        if resource:
            workshop_name = str(resource[0] or "")
        code = REASON_OK if binding else REASON_NO_WAREHOUSE_BINDING
        return WorkshopDiagnosis(
            status="ok" if code == REASON_OK else "problem",
            reason_code=code,
            reason_text=reason_text_for(code, workshop_name=workshop_name),
            recommendation=recommendation_for(code),
            workshop_id=manual_workshop,
            workshop_source="state",
            spec_id=spec_id,
        )

    if not spec_id:
        return no_spec_diagnosis()
    diagnosis = diagnose_specs(db, [spec_id]).get(spec_id)
    return diagnosis or no_spec_diagnosis()


def format_diagnosis_error(prefix: str, diagnosis: WorkshopDiagnosis) -> str:
    """Uniform error text for service-layer refusals."""
    return (
        f"{prefix}: участок не определён — {diagnosis.reason_code}: "
        f"{diagnosis.reason_text}. {diagnosis.recommendation}"
    )
