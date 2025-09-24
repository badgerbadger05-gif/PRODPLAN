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