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

from ..services.planning_service import (
    create_planning_run,
    run_planning_run,
    list_planning_runs,
    get_run_summary,
    get_run_production,
    get_run_purchases,
    get_run_capacity,
    get_run_pegging,
    compute_planning_preview,
    compute_gross_requirements,
    # Config management
    list_planning_configs,
    create_planning_config_version,
    activate_planning_config_version,
    get_active_planning_config_full,
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
    use_weekly: Optional[bool] = None
    config_overrides: Optional[Dict[str, Any]] = None
    started_by: Optional[str] = None


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
            use_weekly=req.use_weekly,
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
            use_weekly=req.use_weekly,
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
            use_weekly=req.use_weekly,
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


@router.get("/stages")
async def get_stages(db: Session = Depends(get_db)):
    """Получить список этапов производства"""
    try:
        return fetch_stages(db)
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