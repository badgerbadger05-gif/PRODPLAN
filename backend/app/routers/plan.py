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
    # retention & pin control
    cleanup_planning_runs,
    set_run_pinned,
    # Config management
    list_planning_configs,
    create_planning_config_version,
    activate_planning_config_version,
    get_active_planning_config_full,
    # Grouped/agenda/summary endpoints
    get_run_production_grouped,
    get_run_production_agenda_day,
    get_run_purchases_grouped,
    get_capacity_summary,
    generate_shortage_report,
)
from ..models import ProductionResource

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


@router.post("/runs/{run_id}/pin")
async def pin_planning_run(
    run_id: int,
    req: PinRequest,
    db: Session = Depends(get_db),
):
    """
    Установить/снять флаг 'pinned' у прогона, чтобы защитить его от авто‑очистки.
    """
    try:
        return set_run_pinned(db=db, run_id=int(run_id), pinned=bool(req.pinned))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/cleanup")
async def cleanup_runs(
    req: RetentionCleanupRequest,
    db: Session = Depends(get_db),
):
    """
    Удалить прогоны старше N дней (по умолчанию 30), кроме помеченных pinned=True.
    Поддерживает dry_run для предварительного отчёта.
    """
    try:
        return cleanup_planning_runs(
            db=db,
            older_than_days=int(req.older_than_days or 30),
            dry_run=bool(req.dry_run),
        )
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

# === Export endpoints for results ===

@router.get("/results/{run_id}/production/export")
async def export_planning_result_production(
    run_id: int,
    format: str = "csv",
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    day_date: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Экспорт результатов «Производство» в CSV или XLSX (base64).
    Колонки: Наименование, Артикул, Количество, ЕИ, Норматив, ч/шт, Норматив всего, ч

    Особый случай (поддержка «Задания на день»):
    - При наличии day_date экспорт строится по данным [services.get_run_production_agenda_day]
      с маппингом:
        Количество = display_qty (если задано) иначе qty
        Норматив всего, ч = display_norm_hours_total (если задано) иначе norm_hours_total
      Групповые подзаголовки используют norm_sum_hours за день.
    """
    try:
        # Базовые колонки данных (как в исходной таблице)
        headers = ["Наименование", "Артикул", "Количество", "ЕИ", "Норматив, ч/шт", "Норматив всего, ч"]

        # Экспорт «Задание на день», если передан day_date
        if (day_date or "").strip():
            # Получаем агрегат «повестка дня» с сервера
            resp = get_run_production_agenda_day(
                db=db,
                run_id=int(run_id),
                day_date=str(day_date)[:10],
                area_id=None,
            )
            groups = (resp or {}).get("groups", []) or []

            def _qty_out(row: Dict[str, Any]) -> float:
                try:
                    if row.get("display_qty") is not None:
                        return float(row.get("display_qty") or 0.0)
                    return float(row.get("qty") or 0.0)
                except Exception:
                    return 0.0

            def _norm_total_out(row: Dict[str, Any]) -> float:
                try:
                    if row.get("display_norm_hours_total") is not None:
                        return float(row.get("display_norm_hours_total") or 0.0)
                    return float(row.get("norm_hours_total") or 0.0)
                except Exception:
                    return 0.0

            # Дедупликация строк «повестки дня» по agg_key (item_id|unit) с приоритетом:
            # overload > наличие display_* > ненулевые показатели
            def _score_order(x: Dict[str, Any]) -> int:
                s = 0
                try:
                    if bool(x.get("overload")):
                        s += 10
                except Exception:
                    pass
                if x.get("display_qty") is not None or x.get("display_norm_hours_total") is not None:
                    s += 5
                try:
                    if float(x.get("display_qty", x.get("qty") or 0.0)) > 0:
                        s += 1
                except Exception:
                    pass
                try:
                    if float(x.get("display_norm_hours_total", x.get("norm_hours_total") or 0.0)) > 0:
                        s += 1
                except Exception:
                    pass
                return s

            def _dedup_orders(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                by_key: Dict[str, Dict[str, Any]] = {}
                for o in (orders or []):
                    k = str(o.get("agg_key") or f"{o.get('item_id')}|{o.get('unit') or ''}")
                    cur = by_key.get(k)
                    if cur is None:
                        by_key[k] = dict(o)
                        continue
                    if _score_order(o) > _score_order(cur):
                        by_key[k] = dict(o)
                    else:
                        # мягкое слияние display_* и флага overload
                        if cur.get("display_qty") is None and o.get("display_qty") is not None:
                            cur["display_qty"] = o.get("display_qty")
                        if cur.get("display_norm_hours_total") is None and o.get("display_norm_hours_total") is not None:
                            cur["display_norm_hours_total"] = o.get("display_norm_hours_total")
                        cur["overload"] = bool(cur.get("overload") or o.get("overload"))
                return list(by_key.values())

            if (format or "csv").lower() == "xlsx":
                import io, base64
                try:
                    from openpyxl import Workbook
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"openpyxl not available: {e}")
                from openpyxl.styles import Font, PatternFill

                wb = Workbook()
                ws = wb.active
                ws.title = "Production Day"
                # Строка заголовков
                ws.append(headers)

                total_records = 0
                for g in groups:
                    area_name = g.get("area_name") or ""
                    items = (g.get("orders") or [])
                    # Дедупликация как в UI «Повестка дня»
                    deduped = _dedup_orders(items)
                    # Заголовок группы с нормо‑часами дня из агрегата
                    group_norm = float(g.get("norm_sum_hours") or 0.0)
                    title = f"Производственный участок: {area_name} · Позиции: {len(deduped)} · Норматив дня: {group_norm:.3f} ч"
                    ws.append([title])
                    r = ws.max_row
                    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
                    cell = ws.cell(row=r, column=1)
                    cell.font = Font(bold=True)
                    cell.fill = PatternFill("solid", fgColor="FFDDDDDD")

                    for x in deduped:
                        ws.append([
                            x.get("item_name") or "",
                            x.get("item_article") or "",
                            _qty_out(x),
                            x.get("unit") or "",
                            float(x.get("norm_hours_per_unit") or 0.0) if x.get("norm_hours_per_unit") is not None else 0.0,
                            _norm_total_out(x),
                        ])
                        total_records += 1
                    ws.append([])  # разделитель групп

                bio = io.BytesIO()
                wb.save(bio)
                bio.seek(0)
                b64 = base64.b64encode(bio.read()).decode("utf-8")
                return {
                    "status": "ok",
                    "format": "xlsx",
                    "data_base64": b64,
                    "filename": f"mrp_production_run_{run_id}.xlsx",
                    "total_rows": int(total_records),
                }
            else:
                # CSV
                import io, csv
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(headers)

                total_records = 0
                for g in groups:
                    area_name = g.get("area_name") or ""
                    items = (g.get("orders") or [])
                    deduped = _dedup_orders(items)
                    group_norm = float(g.get("norm_sum_hours") or 0.0)
                    group_title = f"Производственный участок: {area_name} · Позиции: {len(deduped)} · Норматив дня: {group_norm:.3f} ч"
                    writer.writerow([group_title] + [""] * (len(headers) - 1))

                    for x in deduped:
                        writer.writerow([
                            x.get("item_name") or "",
                            x.get("item_article") or "",
                            _qty_out(x),
                            x.get("unit") or "",
                            float(x.get("norm_hours_per_unit") or 0.0) if x.get("norm_hours_per_unit") is not None else 0.0,
                            _norm_total_out(x),
                        ])
                        total_records += 1
                    writer.writerow([])

                return {
                    "status": "ok",
                    "format": "csv",
                    "data": output.getvalue(),
                    "filename": f"mrp_production_run_{run_id}.csv",
                    "total_rows": int(total_records),
                }

        # Иначе — стандартный экспорт по заказам с группировкой по «доминирующему участку»
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

        # Карта названий участков (ресурсов) для разбивки по «цехам/участкам»
        try:
            resources = db.query(ProductionResource).all()
            area_name_map = {int(x.resource_id): str(x.resource_name or "") for x in resources}
        except Exception:
            area_name_map = {}

        def _dominant_area_name(stages_list):
            dom = None
            for s in (stages_list or []):
                try:
                    if dom is None or float(s.get("hours") or 0.0) > float(dom.get("hours") or 0.0):
                        dom = s
                except Exception:
                    continue
            if dom is None:
                return ""
            aid = None
            try:
                aid = int(dom.get("area_id"))
            except Exception:
                aid = None
            if aid is None:
                return ""
            return area_name_map.get(aid, f"Участок #{aid}")

        # Группировка по «доминирующему» участку как в UI (подзаголовки)
        groups: Dict[str, list] = {}
        for r in rows:
            area_name = _dominant_area_name(r.get("stages"))
            key = area_name or "—"
            groups.setdefault(key, []).append(r)

        grouped_keys = sorted(groups.keys(), key=lambda x: (x == "—", x))
        # Количество строк данных (без учёта подзаголовков)
        total_records = sum(len(groups.get(k, [])) for k in grouped_keys)

        if (format or "csv").lower() == "xlsx":
            # Генерация XLSX
            import io, base64
            try:
                from openpyxl import Workbook
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"openpyxl not available: {e}")
            from openpyxl.styles import Font, PatternFill
            wb = Workbook()
            ws = wb.active
            ws.title = "Production"
            ws.append(headers)
            for area_name in grouped_keys:
                items = groups.get(area_name, [])
                total_norm = sum(float(x.get("norm_hours_total") or 0.0) for x in items)
                title = f"Производственный участок: {area_name} · Заказов: {len(items)} · Норматив всего: {total_norm:.3f} ч"
                ws.append([title])
                r = ws.max_row
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
                cell = ws.cell(row=r, column=1)
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="FFDDDDDD")
                for x in items:
                    ws.append([
                        x.get("item_name") or "",
                        x.get("item_article") or "",
                        float(x.get("qty") or 0.0),
                        x.get("unit") or "",
                        float(x.get("norm_hours_per_unit") or 0.0) if x.get("norm_hours_per_unit") is not None else 0.0,
                        float(x.get("norm_hours_total") or 0.0),
                    ])
                ws.append([])
            bio = io.BytesIO()
            wb.save(bio)
            bio.seek(0)
            b64 = base64.b64encode(bio.read()).decode("utf-8")
            return {
                "status": "ok",
                "format": "xlsx",
                "data_base64": b64,
                "filename": f"mrp_production_run_{run_id}.xlsx",
                "total_rows": int(total_records),
            }
        else:
            # CSV
            import io, csv
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            for area_name in grouped_keys:
                items = groups.get(area_name, [])
                total_norm = sum(float(x.get("norm_hours_total") or 0.0) for x in items)
                group_title = f"Производственный участок: {area_name} · Заказов: {len(items)} · Норматив всего: {total_norm:.3f} ч"
                writer.writerow([group_title] + [""] * (len(headers) - 1))
                for x in items:
                    writer.writerow([
                        x.get("item_name") or "",
                        x.get("item_article") or "",
                        float(x.get("qty") or 0.0),
                        x.get("unit") or "",
                        float(x.get("norm_hours_per_unit") or 0.0) if x.get("norm_hours_per_unit") is not None else 0.0,
                        float(x.get("norm_hours_total") or 0.0),
                    ])
                writer.writerow([])
            return {
                "status": "ok",
                "format": "csv",
                "data": output.getvalue(),
                "filename": f"mrp_production_run_{run_id}.csv",
                "total_rows": int(total_records),
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
            import io, base64
            try:
                from openpyxl import Workbook
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"openpyxl not available: {e}")
            wb = Workbook()
            ws = wb.active
            ws.title = "Purchases"
            ws.append(headers)
            for row in data_rows:
                ws.append(row)
            bio = io.BytesIO()
            wb.save(bio)
            bio.seek(0)
            b64 = base64.b64encode(bio.read()).decode("utf-8")
            return {
                "status": "ok",
                "format": "xlsx",
                "data_base64": b64,
                "filename": f"mrp_purchases_run_{run_id}.xlsx",
                "total_rows": len(data_rows),
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
                "filename": f"mrp_purchases_run_{run_id}.csv",
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

# === Backend-first aggregated/grouped endpoints (non-breaking, additive) ===

@router.get("/results/{run_id}/production/grouped")
async def get_planning_result_production_grouped(
    run_id: int,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    area_id: Optional[int] = None,
    limit: int = 1000,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Группировка производственных заказов по «виду/участку» (dominant area),
    с серверной агрегацией по (item_id, unit) и индикаторами мощности.
    Не ломает существующие endpoints; добавлен отдельно.
    """
    try:
        return get_run_production_grouped(
            db=db,
            run_id=int(run_id),
            bucket_type=bucket_type,
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


@router.get("/results/{run_id}/production/agenda_day")
async def get_planning_result_production_agenda_day(
    run_id: int,
    day_date: str,
    area_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """
    Задание на конкретный день (daily) по видам/участкам.
    Пересчёт: часы → количество по норме на штуку, вычисленной на сервере.
    """
    try:
        return get_run_production_agenda_day(
            db=db,
            run_id=int(run_id),
            day_date=day_date,
            area_id=area_id,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/results/{run_id}/purchases/grouped")
async def get_planning_result_purchases_grouped(
    run_id: int,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """
    Сводная группировка закупок по (item_id, unit) на сервере.
    """
    try:
        return get_run_purchases_grouped(
            db=db,
            run_id=int(run_id),
            bucket_type=bucket_type,
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
    """
    Generate and return an XLSX shortage report for a given planning run.
    """
    try:
        return generate_shortage_report(db=db, run_id=run_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/results/{run_id}/capacity/summary")
async def get_planning_result_capacity_summary(
    run_id: int,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Сводка по мощности (часы/перегрузы) по видам/участкам в заданном диапазоне.
    """
    try:
        return get_capacity_summary(
            db=db,
            run_id=int(run_id),
            bucket_type=bucket_type,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))