from datetime import date
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from ..database import get_db
from ..services.plan_service import (
    query_plan_matrix_paginated,
    upsert_plan_entry,
    bulk_upsert_plan_entries,
    delete_plan_rows_for_item,
    delete_root_product_for_item,
    ensure_root_product_by_code,
    fetch_stages
)

from ..services.production_report_service import (
    get_week_report,
    bulk_upsert_fact,
    close_previous_workday,
    get_planning_anchor_date,
)

from ..services.planning_service import (
    run_planning_run,
    list_planning_runs,
    get_run_summary,
    get_run_production,
    get_run_production_grouped,
    get_run_purchases,
    get_run_purchases_grouped_by_category,
    get_run_rework,
    get_run_rework_grouped,
    get_run_rework_grouped_by_category,
    get_run_capacity,
    get_run_pegging,
    compute_planning_preview,
    compute_gross_requirements,
    # retention & pin control
    # Config management
    list_planning_configs,
    create_planning_config_version,
    activate_planning_config_version,
    get_active_planning_config_full
)
from ..models import ProductionResource
from ..schemas import ProductionGroupedResponse, PurchaseCategoryGroupedResponse, ReworkGroupedResponse
from ..models import ForcedOrderRequest, ForcedOrderResult

from ..services.forced_orders import (
    create_forced_order_request,
    process_forced_order_request,
    export_forced_order_xlsx,
    export_shortage_report_for_run,
)
from ..services.mrp_result_export import (
    export_purchases_results_xlsx,
    export_rework_results_xlsx,
)

router = APIRouter(prefix="/v1/plan", tags=["plan"])


# Pydantic модели
class PlanMatrixRequest(BaseModel):
    start_date: Optional[str] = None
    days: int = 30
    stage_id: Optional[int] = None
    page: int = 1
    page_size: int = 30
    sort_by: str = 'item_name'
    sort_dir: str = 'asc'


class UpsertPlanRequest(BaseModel):
    item_id: int
    date: str
    qty: int
    stage_id: Optional[int] = None


class BulkUpsertEntry(BaseModel):
    item_id: int
    date: str
    qty: int
    stage_id: Optional[int] = None


class BulkUpsertRequest(BaseModel):
    entries: List[BulkUpsertEntry] = []


class DeleteRowRequest(BaseModel):
    item_id: int
    start_date: Optional[str] = None
    days: int = 30
    stage_id: Optional[int] = None


class EnsureItemRequest(BaseModel):
    item_code: str
    item_name: Optional[str] = None
    item_article: Optional[str] = None


class ExportRequest(BaseModel):
    format: str = 'csv'
    start_date: Optional[str] = None
    days: int = 30
    stage_id: Optional[int] = None


class PlanningConfigCreate(BaseModel):
    config: Dict[str, Any]
    comment: Optional[str] = None
    created_by: Optional[str] = None
    activate: Optional[bool] = False


# ===== Weekly production report (week view + day close) =====


class ProductionReportWeekRequest(BaseModel):
    week_start: Optional[str] = None
    any_date_in_week: Optional[str] = None


class ProductionReportFactEntry(BaseModel):
    item_id: int
    date: str
    fact_qty: float


class ProductionReportFactBulkUpsertRequest(BaseModel):
    entries: List[ProductionReportFactEntry] = []
    rerun_editable_date: Optional[str] = None


class ProductionReportDayCloseRequest(BaseModel):
    close_date: Optional[str] = None
    closed_by: Optional[str] = None


# ===== Forced orders (manual/override) =====


class ForcedOrderCreateRequest(BaseModel):
    run_id: Optional[int] = None
    item_id: int
    need_date: str
    requested_qty: float
    created_by: Optional[str] = None
    reason: Optional[str] = None



@router.post("/matrix")
async def get_plan_matrix(
    req: PlanMatrixRequest,
    db: Session = Depends(get_db)
):
    """Получить матрицу плана производства по дням"""
    return query_plan_matrix_paginated(
        start_date_str=req.start_date or date.today().isoformat(),
        days=int(req.days or 30),
        stage_id=req.stage_id,
        page=int(req.page or 1),
        page_size=int(req.page_size or 30),
        sort_by=req.sort_by or 'item_name',
        sort_dir=req.sort_dir or 'asc',
        db=db,
    )


@router.post("/upsert")
async def upsert_plan(
    req: UpsertPlanRequest,
    db: Session = Depends(get_db)
):
    """Добавить/обновить запись плана производства"""
    try:
        upsert_plan_entry(
            item_id=int(req.item_id),
            date_str=str(req.date),
            planned_qty=float(req.qty or 0),
            stage_id=req.stage_id,
            db=db,
        )
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/bulk_upsert")
async def bulk_upsert_plan(
    req: BulkUpsertRequest,
    db: Session = Depends(get_db)
):
    """Пакетное сохранение записей плана производства"""
    try:
        payload = [
            {
                'item_id': int(e.item_id),
                'date': str(e.date),
                'qty': int(e.qty),
                'stage_id': e.stage_id,
            }
            for e in (req.entries or [])
        ]
        saved = bulk_upsert_plan_entries(payload, db=db)
        return {"status": "ok", "saved": int(saved)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/production_report/week")
async def get_production_report_week(
    req: ProductionReportWeekRequest,
    db: Session = Depends(get_db),
):
    """Недельный отчёт о выпуске техники (Пн–Вс), включая статусы закрытия дней."""
    try:
        ws = date.fromisoformat(req.week_start) if req.week_start else None
        ad = date.fromisoformat(req.any_date_in_week) if req.any_date_in_week else None
        return get_week_report(db=db, week_start=ws, any_date_in_week=ad)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/production_report/fact/bulk_upsert")
async def bulk_upsert_production_report_fact(
    req: ProductionReportFactBulkUpsertRequest,
    db: Session = Depends(get_db),
):
    """Пакетное сохранение факта выпуска (completed_qty).

    Важно: закрытые дни read-only.
    """
    try:
        rerun_editable_date = date.fromisoformat(req.rerun_editable_date) if req.rerun_editable_date else None
        payload = [
            {"item_id": int(e.item_id), "date": str(e.date), "fact_qty": float(e.fact_qty or 0.0)}
            for e in (req.entries or [])
        ]
        saved = bulk_upsert_fact(db=db, entries=payload, rerun_editable_date=rerun_editable_date)
        db.commit()
        return {"status": "ok", "saved": int(saved)}
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/production_report/day/close")
async def close_production_report_day(
    req: ProductionReportDayCloseRequest,
    db: Session = Depends(get_db),
):
    """Закрыть предыдущий рабочий день: перенос остатка (carry) на D_target.

    Поддерживает re-run (повторное закрытие) с откатом предыдущего переноса.
    """
    try:
        close_date = date.fromisoformat(req.close_date) if req.close_date else None
        result = close_previous_workday(db=db, closed_by=req.closed_by, close_date_override=close_date)
        db.commit()
        return result
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/anchor")
async def get_planning_anchor(
    db: Session = Depends(get_db),
):
    """Якорная дата для отображения планового окна.

    Семантика: первый НЕ закрытый рабочий день после последнего закрытого.
    """
    try:
        return get_planning_anchor_date(db=db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/delete_row")
async def delete_plan_row(
    req: DeleteRowRequest,
    db: Session = Depends(get_db)
):
    """Удалить записи плана для изделия в заданном периоде"""
    try:
        start_date = req.start_date or date.today().isoformat()
        deleted = delete_plan_rows_for_item(
            start_date_str=start_date,
            days=int(req.days or 30),
            item_id=int(req.item_id),
            stage_id=req.stage_id,
            db=db,
        )
        # Дополнительно удаляем строку из root_products, чтобы изделие исчезло из матрицы
        root_deleted = delete_root_product_for_item(
            item_id=int(req.item_id),
            db=db,
        )
        return {"status": "ok", "deleted": int(deleted), "root_deleted": int(root_deleted)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/ensure_item")
async def ensure_plan_item(
    req: EnsureItemRequest,
    db: Session = Depends(get_db)
):
    """Гарантировать наличие изделия в плане производства"""
    try:
        item_id = ensure_root_product_by_code(
            item_code=str(req.item_code).strip(),
            item_name=req.item_name,
            item_article=req.item_article,
            db=db,
        )
        return {"status": "ok", "item_id": int(item_id)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===== MRP Planning API (runs and results) =====

class CalcRequest(BaseModel):
    horizon_days: Optional[int] = None
    config_overrides: Optional[Dict[str, Any]] = None
    started_by: Optional[str] = None


# Retention & pinning DTOs
class RetentionCleanupRequest(BaseModel):
    older_than_days: Optional[int] = 30
    dry_run: Optional[bool] = False


class PinRequest(BaseModel):
    pinned: bool


@router.post("/calc")
async def start_planning_run(
    req: CalcRequest,
    db: Session = Depends(get_db)
):
    """
    Полный расчёт планирования и сохранение результатов прогона.
    Создаёт RUN с конфигурацией (RUNNING) → выполняет расчёт предпросмотра (gross+net),
    классифицирует потоки (production/purchase), сохраняет planned_order/planned_purchase,
    завершает статусом SUCCESS/FAILED и возвращает run_id.
    """
    try:
        run_id = run_planning_run(
            db=db,
            horizon_days=req.horizon_days,
            config_overrides=req.config_overrides or {},
            started_by=req.started_by or "api",
        )
        return {"status": "ok", "run_id": int(run_id)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/calc_preview")
async def calc_preview(
    req: CalcRequest,
    db: Session = Depends(get_db)
):
    """
    Предпросчёт валовой и чистой потребности (без записи результатов в БД).
    Политики и горизонты берутся из активной конфигурации с учётом overrides.
    """
    try:
        result = compute_planning_preview(
            db=db,
            horizon_days=req.horizon_days,
            config_overrides=req.config_overrides or {},
        )
        return {"status": "ok", "preview": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/calc_gross")
async def calc_gross(
    req: CalcRequest,
    db: Session = Depends(get_db)
):
    """
    Валовая потребность (BOM-развёртка) без неттинга и без записи в БД.
    """
    try:
        result = compute_gross_requirements(
            db=db,
            horizon_days=req.horizon_days,
            config_overrides=req.config_overrides or {},
        )
        return {"status": "ok", "gross": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===== Planning configuration management API =====

@router.get("/configs")
async def list_configs(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Список версий конфигурации планирования (пагинация)"""
    try:
        return list_planning_configs(db=db, limit=limit, offset=offset)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/configs/active")
async def get_active_config(db: Session = Depends(get_db)):
    """Получить активную конфигурацию планирования (полный JSON снапшот)"""
    try:
        data = get_active_planning_config_full(db=db)
        return {"status": "ok", "config": data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/configs")
async def create_config(
    req: PlanningConfigCreate,
    db: Session = Depends(get_db)
):
    """Создать новую версию конфигурации планирования (опционально сразу активировать)"""
    try:
        result = create_planning_config_version(
            db=db,
            config=req.config,
            comment=req.comment,
            created_by=req.created_by,
            activate=bool(req.activate or False),
        )
        return {"status": "ok", "created": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/configs/{config_id}/activate")
async def activate_config(
    config_id: int,
    db: Session = Depends(get_db)
):
    """Активировать указанную версию конфигурации (сняв active с предыдущей)"""
    try:
        result = activate_planning_config_version(db=db, config_id=int(config_id))
        return {"status": "ok", "activated": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))




@router.get("/runs")
async def get_planning_runs(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Список прогонов планирования с краткой статистикой"""
    try:
        return list_planning_runs(db=db, limit=limit, offset=offset)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}")
async def get_planning_result_summary(
    run_id: int,
    db: Session = Depends(get_db)
):
    """Сводка результатов прогона планирования (KPI, предупреждения, базовые счётчики)"""
    try:
        return get_run_summary(db=db, run_id=int(run_id))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/production")
async def get_planning_result_production(
    run_id: int,
    item_id: Optional[int] = None,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Производственные заказы и этапы по прогону (с фильтрами и пагинацией)"""
    try:
        return get_run_production(
            db=db,
            run_id=int(run_id),
            item_id=item_id,
            bucket_type=bucket_type,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/results/{run_id}/production/grouped", response_model=ProductionGroupedResponse)
async def get_planning_result_production_grouped(
    run_id: int,
    item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    area_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Группированная по участкам выдача производственных заказов"""
    try:
        return get_run_production_grouped(
            db=db,
            run_id=int(run_id),
            item_id=item_id,
            date_from=date_from,
            date_to=date_to,
            area_id=area_id,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/purchases")
async def get_planning_result_purchases(
    run_id: int,
    item_id: Optional[int] = None,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Заявки на закупку по прогону (с фильтрами и пагинацией)"""
    try:
        return get_run_purchases(
            db=db,
            run_id=int(run_id),
            item_id=item_id,
            bucket_type=bucket_type,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/purchases/grouped")
async def get_planning_result_purchases_grouped(
    run_id: int,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Агрегированная выдача закупок по item_id+unit (для верхней таблицы UI)."""
    try:
        # Используем существующую агрегацию/денормализацию из сервиса.
        # Берём большой лимит, затем режем пагинацией на уровне роутера.
        res = get_run_purchases(
            db=db,
            run_id=int(run_id),
            item_id=None,
            bucket_type=None,
            date_from=date_from,
            date_to=date_to,
            limit=100000,
            offset=0,
            sort_by="item_name",
            sort_dir="asc",
        )
        base_rows = (res or {}).get("rows", []) or []

        mapped = []
        for r in base_rows:
            try:
                iid = int(r.get("item_id"))
            except Exception:
                continue
            unit = r.get("unit")
            mapped.append(
                {
                    "agg_key": f"{iid}|{unit or ''}",
                    "item_id": iid,
                    "item_name": r.get("item_name"),
                    "item_article": r.get("item_article"),
                    "unit": unit,
                    "qty": float(r.get("qty") or 0.0),
                }
            )

        total = int(len(mapped))
        eff_limit = max(1, min(int(limit or 1000), 5000))
        eff_offset = max(0, int(offset or 0))
        page = mapped[eff_offset : eff_offset + eff_limit]
        return {"rows": page, "total": total, "limit": eff_limit, "offset": eff_offset}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/rework")
async def get_planning_result_rework(
    run_id: int,
    item_id: Optional[int] = None,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Заказы на переработку по прогону (с фильтрами и пагинацией)."""
    try:
        return get_run_rework(
            db=db,
            run_id=int(run_id),
            item_id=item_id,
            bucket_type=bucket_type,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/rework/grouped", response_model=ReworkGroupedResponse)
async def get_planning_result_rework_grouped(
    run_id: int,
    item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Группированная выдача заказов на переработку."""
    try:
        return get_run_rework_grouped(
            db=db,
            run_id=int(run_id),
            item_id=item_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/purchases/grouped-by-category", response_model=PurchaseCategoryGroupedResponse)
async def get_planning_result_purchases_grouped_by_category(
    run_id: int,
    item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Группированная выдача закупок по товарным группам."""
    try:
        return get_run_purchases_grouped_by_category(
            db=db,
            run_id=int(run_id),
            item_id=item_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/rework/grouped-by-category", response_model=ReworkGroupedResponse)
async def get_planning_result_rework_grouped_by_category(
    run_id: int,
    item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Группированная выдача заказов на переработку по товарным группам."""
    try:
        return get_run_rework_grouped_by_category(
            db=db,
            run_id=int(run_id),
            item_id=item_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/capacity")
async def get_planning_result_capacity(
    run_id: int,
    area_id: Optional[int] = None,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Загрузка мощностей по участкам (фильтры и пагинация)"""
    try:
        return get_run_capacity(
            db=db,
            run_id=int(run_id),
            area_id=area_id,
            bucket_type=bucket_type,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/pegging")
async def get_planning_result_pegging(
    run_id: int,
    child_item_id: Optional[int] = None,
    parent_item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Трассируемость компонент → спрос (pegging) по прогону (с фильтрами и пагинацией)"""
    try:
        return get_run_pegging(
            db=db,
            run_id=int(run_id),
            child_item_id=child_item_id,
            parent_item_id=parent_item_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/shortage-report")
async def get_shortage_report(
    run_id: int,
    db: Session = Depends(get_db),
):
    """XLSX отчёт по дефицитам комплектующих (blocked/partial) для прогона"""
    try:
        return export_shortage_report_for_run(db=db, run_id=int(run_id))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/forced_orders")
async def create_forced_order(
    req: ForcedOrderCreateRequest,
    db: Session = Depends(get_db),
):
    """Создать заявку на «принудительный заказ» (не влияет на основной MRP run)"""
    try:
        rec = create_forced_order_request(
            db,
            run_id=req.run_id,
            item_id=int(req.item_id),
            need_date=str(req.need_date),
            requested_qty=float(req.requested_qty or 0.0),
            created_by=req.created_by,
            reason=req.reason,
        )
        db.commit()
        return {"status": "ok", "request_id": int(rec.id)}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/forced_orders")
async def list_forced_orders(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Список заявок на принудительные заказы"""
    try:
        q = db.query(ForcedOrderRequest).order_by(ForcedOrderRequest.created_at.desc())
        total = q.count()
        rows = q.offset(max(0, int(offset))).limit(max(1, min(int(limit or 50), 500))).all()
        out = []
        for r in rows:
            out.append(
                {
                    "id": int(r.id),
                    "run_id": int(r.run_id) if r.run_id is not None else None,
                    "item_id": int(r.item_id),
                    "need_date": r.need_date.isoformat() if getattr(r, "need_date", None) else None,
                    "requested_qty": float(getattr(r, "requested_qty", 0.0) or 0.0),
                    "status": getattr(r, "status", None),
                    "created_by": getattr(r, "created_by", None),
                    "reason": getattr(r, "reason", None),
                    "created_at": r.created_at.isoformat() if getattr(r, "created_at", None) else None,
                }
            )
        return {"status": "ok", "rows": out, "total": int(total), "limit": int(limit), "offset": int(offset)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/forced_orders/{request_id}/process")
async def process_forced_order(
    request_id: int,
    db: Session = Depends(get_db),
):
    """Посчитать принудительный заказ (комплектующие не блокируют, но дефицит фиксируется в diagnostics)"""
    try:
        result = process_forced_order_request(db=db, request_id=int(request_id))
        db.commit()
        return {"status": "ok", "data": result}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/forced_orders/{request_id}/export")
async def export_forced_order(
    request_id: int,
    db: Session = Depends(get_db),
):
    """Экспорт принудительного заказа в XLSX (base64)"""
    try:
        return export_forced_order_xlsx(db=db, request_id=int(request_id))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/stages")
async def get_stages(db: Session = Depends(get_db)):
    """Получить список этапов производства"""
    try:
        return fetch_stages(db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# === Export endpoints for results ===


@router.get("/results/{run_id}/production/export")
async def export_planning_result_production(
    run_id: int,
    format: str = "csv",
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Экспорт результатов «Производство» в CSV или XLSX (base64).
    Колонки: Наименование, Артикул, Количество, Нормо-часы всего, Нормо-часы на ед., Дата потребности, Дата начала, Дата окончания, ЕИ
    """
    try:
        res = get_run_production(
            db=db,
            run_id=int(run_id),
            item_id=None,
            bucket_type=bucket_type,
            date_from=date_from,
            date_to=date_to,
            limit=100000,
            offset=0,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
        rows = res.get("rows", []) or []

        headers = [
            "Наименование",
            "Артикул",
            "Количество",
            "Нормо-часы всего",
            "Нормо-часы на ед.",
            "Дата потребности",
            "Дата начала",
            "Дата окончания",
            "ЕИ",
        ]
        data_rows = []
        for r in rows:
            qty = float(r.get("qty") or 0.0)
            norm_total = float(r.get("norm_hours_total") or 0.0)
            norm_per_unit = r.get("norm_hours_per_unit")
            if norm_per_unit is None:
                norm_per_unit = (norm_total / qty) if qty > 0 else None
            data_rows.append(
                [
                    r.get("item_name") or "",
                    r.get("item_article") or "",
                    qty,
                    norm_total,
                    float(norm_per_unit) if norm_per_unit is not None else "",
                    r.get("need_date") or "",
                    r.get("start_date") or "",
                    r.get("finish_date") or "",
                    r.get("unit") or "",
                ]
            )

        if (format or "csv").lower() == "xlsx":
            # Формируем XLSX с разбивкой по участкам (подзаголовки)
            # Пытаемся получить серверную группировку; при ошибке/пусто — фолбэк к плоскому списку
            try:
                grouped_res = get_run_production_grouped(
                    db=db,
                    run_id=int(run_id),
                    item_id=None,
                    date_from=date_from,
                    date_to=date_to,
                    area_id=None,
                    limit=1000,
                    offset=0,
                    sort_by=sort_by,
                    sort_dir=sort_dir,
                )
                groups = (grouped_res or {}).get("groups", []) or []
            except Exception:
                groups = []

            import io, base64
            try:
                from openpyxl import Workbook
                from openpyxl.styles import PatternFill, Font, Alignment
                from openpyxl.utils import get_column_letter
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"openpyxl not available: {e}")

            wb = Workbook()
            ws = wb.active
            ws.title = "Production"

            # Трекинг максимальной ширины контента по колонкам для псевдо-автоширины
            max_widths = {i: len(str(h)) for i, h in enumerate(headers, start=1)}

            def update_widths(values: list):
                for idx, val in enumerate(values, start=1):
                    text = "" if val is None else str(val)
                    # Учитываем переносы строк по наибольшей длине строки
                    length = max((len(line) for line in str(text).splitlines()), default=0)
                    if length > max_widths.get(idx, 0):
                        max_widths[idx] = length

            def style_header(row_idx: int):
                for col_idx in range(1, len(headers) + 1):
                    cell = ws.cell(row=row_idx, column=col_idx)
                    cell.font = Font(bold=True)

            def append_group_title(title: str):
                ws.append([title])
                r = ws.max_row
                # Подзаголовок на всю ширину таблицы
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
                cell = ws.cell(row=r, column=1)
                cell.font = Font(bold=True, color="FFFFFFFF")
                # Яркий синий фон для подзаголовка группы
                cell.fill = PatternFill(fill_type="solid", fgColor="FF4F81BD")
                cell.alignment = Alignment(horizontal="left")
                update_widths([title])

            if groups:
                # Для каждой группы добавляем подзаголовок с названием участка, затем шапку и строки
                for g in groups:
                    area_name = str(g.get("area_name") or f"ID {g.get('area_id') or ''}")
                    # Подзаголовок группы
                    append_group_title(f"Участок: {area_name}")
                    # Шапка колонок
                    ws.append(headers)
                    hdr_row = ws.max_row
                    style_header(hdr_row)
                    update_widths(headers)
                    orders = (g.get("orders", []) or [])
                    for o in orders:
                        qty = float(o.get("qty") or 0.0)
                        norm_total = float(o.get("norm_hours_total") or 0.0)
                        npu = o.get("norm_hours_per_unit")
                        if npu is None:
                            npu = (norm_total / qty) if qty > 0 else None
                        row_values = [
                            o.get("item_name") or "",
                            o.get("item_article") or "",
                            qty,
                            norm_total,
                            float(npu) if npu is not None else "",
                            "",  # Дата потребности (в агрегате может отсутствовать)
                            "",  # Дата начала
                            "",  # Дата окончания
                            o.get("unit") or "",
                        ]
                        ws.append(row_values)
                        update_widths(row_values)
                    # Пустая строка между группами
                    ws.append([])
            else:
                # Фолбэк: плоский список без группировки
                ws.append(headers)
                hdr_row = ws.max_row
                style_header(hdr_row)
                update_widths(headers)
                for row in data_rows:
                    ws.append(row)
                    update_widths(row)

            # Установка ширины колонок в зависимости от контента (псевдо-автоширина)
            for col_idx in range(1, len(headers) + 1):
                letter = get_column_letter(col_idx)
                width = max_widths.get(col_idx, 10)
                # Коэффициент подбора ширины + небольшой запас, ограничения разумных пределов
                adjusted = min(max(width * 1.2 + 2, 12), 60)
                ws.column_dimensions[letter].width = adjusted

            bio = io.BytesIO()
            wb.save(bio)
            bio.seek(0)
            b64 = base64.b64encode(bio.read()).decode("utf-8")
            return {
                "status": "ok",
                "format": "xlsx",
                "data_base64": b64,
                "filename": f"mrp_production_run_{run_id}.xlsx",
                "total_rows": len(data_rows) if not groups else sum(len((g.get("orders") or [])) for g in groups),
            }
        else:
            import io, csv
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            for row in data_rows:
                writer.writerow(row)
            return {
                "status": "ok",
                "format": "csv",
                "data": output.getvalue(),
                "filename": f"mrp_production_run_{run_id}.csv",
                "total_rows": len(data_rows),
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
@router.get("/results/{run_id}/purchases/export")
async def export_planning_result_purchases(
    run_id: int,
    format: str = "csv",
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Экспорт результатов «Закупки» в CSV или XLSX (base64).
    Колонки: Наименование, Артикул, Количество, ЕИ
    """
    try:
        res = get_run_purchases(
            db=db,
            run_id=int(run_id),
            item_id=None,
            bucket_type=bucket_type,
            date_from=date_from,
            date_to=date_to,
            limit=100000,
            offset=0,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
        rows = res.get("rows", []) or []

        headers = ["Наименование", "Артикул", "Количество", "ЕИ"]
        data_rows = []
        for r in rows:
            data_rows.append([
                r.get("item_name") or "",
                r.get("item_article") or "",
                float(r.get("qty") or 0.0),
                r.get("unit") or "",
            ])

        if (format or "csv").lower() == "xlsx":
            return export_purchases_results_xlsx(
                db=db,
                run_id=int(run_id),
                item_id=None,
                date_from=date_from,
                date_to=date_to,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )
        else:
            import io, csv
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            for row in data_rows:
                writer.writerow(row)
            return {
                "status": "ok",
                "format": "csv",
                "data": output.getvalue(),
                "filename": f"mrp_purchases_run_{run_id}.csv",
                "total_rows": len(data_rows),
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/rework/export")
async def export_planning_result_rework(
    run_id: int,
    format: str = "csv",
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Экспорт результатов «Переработка» в CSV или XLSX (base64).
    XLSX группируется по товарным группам.
    """
    try:
        res = get_run_rework(
            db=db,
            run_id=int(run_id),
            item_id=None,
            bucket_type=bucket_type,
            date_from=date_from,
            date_to=date_to,
            limit=100000,
            offset=0,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
        rows = res.get("rows", []) or []

        headers = [
            "Наименование",
            "Артикул",
            "Количество",
            "Запрошено",
            "К плану",
            "ЕИ",
            "Дата потребности",
            "Дата запуска",
            "Срок пополнения, дн.",
            "Спецификация",
            "Лимит по комплектующим",
            "Статус комплектующих",
        ]
        data_rows = []
        for r in rows:
            if bool(r.get("component_blocked")):
                status = "Заблокирован"
            elif bool(r.get("component_partial")):
                status = "Частично ограничен"
            else:
                status = "Без ограничений"

            data_rows.append([
                r.get("item_name") or "",
                r.get("item_article") or "",
                float(r.get("qty") or 0.0),
                float(r.get("requested_qty") or 0.0),
                float(r.get("planned_qty") or 0.0),
                r.get("unit") or "",
                r.get("need_date") or "",
                r.get("order_date") or "",
                int(r.get("lead_time_days") or 0),
                r.get("spec_name") or r.get("spec_code") or "",
                float(r.get("component_limit") or 0.0) if r.get("component_limit") is not None else "",
                status,
            ])

        if (format or "csv").lower() == "xlsx":
            return export_rework_results_xlsx(
                db=db,
                run_id=int(run_id),
                item_id=None,
                date_from=date_from,
                date_to=date_to,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )

        import io, csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        for row in data_rows:
            writer.writerow(row)
        return {
            "status": "ok",
            "format": "csv",
            "data": output.getvalue(),
            "filename": f"mrp_rework_run_{run_id}.csv",
            "total_rows": len(data_rows),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/export")
async def export_plan(
    req: ExportRequest,
    db: Session = Depends(get_db)
):
    """Экспортировать план производства в CSV или Excel"""
    try:
        # Получаем все данные без пагинации
        data = query_plan_matrix_paginated(
            start_date_str=req.start_date or date.today().isoformat(),
            days=int(req.days or 30),
            stage_id=req.stage_id,
            page=1,
            page_size=10000,  # Большое число для получения всех данных
            sort_by='item_name',
            sort_dir='asc',
            db=db,
        )

        rows = data.get('rows', [])
        dates = data.get('dates', [])

        # Преобразуем в формат для экспорта
        export_rows = []
        for row in rows:
            export_row = {
                'Изделие': row.get('item_name', ''),
                'Артикул': row.get('item_article', ''),
                'Код': row.get('item_code', ''),
                'План на месяц': row.get('month_plan', 0),
            }

            # Добавляем колонки по дням
            days_data = row.get('days', {})
            for date_str in dates:
                export_row[date_str] = days_data.get(date_str, 0)

            export_rows.append(export_row)

        # Возвращаем CSV
        import io
        import csv

        output = io.StringIO()
        if export_rows:
            writer = csv.DictWriter(output, fieldnames=export_rows[0].keys())
            writer.writeheader()
            writer.writerows(export_rows)

        return {
            "status": "ok",
            "data": output.getvalue(),
            "format": req.format,
            "total_rows": len(export_rows)
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
