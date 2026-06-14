from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List

from ..database import get_db
from ..schemas import ODataSyncRequest, ODataSyncStats
from ..services.odata_stock_sync import sync_stock_from_odata, sync_stock_warehouses_from_odata
from ..services.nomenclature_sync import sync_nomenclature_from_odata, NomenclatureSyncStats
from ..services.category_sync import sync_categories_from_odata, CategorySyncStats
from ..services.specification_sync import sync_specifications_from_odata, SpecificationSyncStats
from ..services.production_order_sync import sync_production_orders_from_odata, ProductionOrderSyncStats, sync_production_fact_from_odata
from ..services.production_order_export import export_production_orders_xlsx
from ..services.supplier_order_sync import sync_supplier_orders_from_odata, SupplierOrderSyncStats
from ..services.supplier_order_export import export_supplier_orders_xlsx
from ..services.default_specification_sync import sync_default_specifications_from_odata, DefaultSpecificationSyncStats
from ..services.production_stage_sync import sync_production_stages_from_odata, ProductionStageSyncStats

from ..services.units_sync import UNIT_CLASSIFIER_ENTITY, UNIT_ENTITY, sync_units_from_odata, backfill_units_from_items
from ..services.operations_sync import sync_operations_from_odata, OperationsSyncStats
from ..services.production_kind_sync import sync_production_kinds_from_odata, ProductionKindSyncStats
from ..services.employee_sync import sync_employees_from_odata
from ..services import sync_orchestrator

from .. import models
from typing import Dict, Optional

router = APIRouter(prefix="/v1/sync", tags=["sync"])


class WarehouseSelectionPayload(BaseModel):
    selected_refs: List[str] = []


class SyncJobConfigPatch(BaseModel):
    interval_seconds: Optional[int] = None
    enabled: Optional[bool] = None


class SyncAutoConfigPayload(BaseModel):
    jobs: Dict[str, SyncJobConfigPatch] = {}


@router.post("/stock-odata", response_model=ODataSyncStats)
def sync_stock_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация остатков из 1С через OData.
    Тело запроса:
    {
      "base_url": "http://srv-1c:8080/base/odata/standard.odata",
      "entity_name": "AccumulationRegister_ЗапасыНаСкладах",
      "username": "user",
      "password": "pass",
      "token": null,
      "filter_query": null,
      "select_fields": null,
      "dry_run": false,
      "zero_missing": false
    }
    """
    try:
        stats = sync_stock_from_odata(db, payload)
        # Приводим к схеме ответа
        return ODataSyncStats(**stats)  # type: ignore[arg-type]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.post("/warehouses-odata", response_model=dict)
def sync_warehouses_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация списка складов из 1С (через тот же регистр остатков).
    Остатки номенклатуры не обновляет.
    """
    try:
        return sync_stock_warehouses_from_odata(db, payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.get("/warehouses", response_model=dict)
def get_stock_warehouses(db: Session = Depends(get_db)):
    rows = (
        db.query(models.StockWarehouse)
        .order_by(models.StockWarehouse.warehouse_name.asc(), models.StockWarehouse.warehouse_code.asc())
        .all()
    )
    return {
        "rows": [
            {
                "warehouse_id": int(r.warehouse_id),
                "warehouse_ref1c": str(r.warehouse_ref1c),
                "warehouse_code": str(r.warehouse_code or ""),
                "warehouse_name": str(r.warehouse_name or ""),
                "is_selected": bool(r.is_selected),
            }
            for r in rows
        ],
        "total": len(rows),
        "selected_total": sum(1 for r in rows if bool(r.is_selected)),
    }


@router.post("/warehouses/selection", response_model=dict)
def save_stock_warehouse_selection(payload: WarehouseSelectionPayload, db: Session = Depends(get_db)):
    selected = {str(x).strip() for x in (payload.selected_refs or []) if str(x).strip()}
    rows = db.query(models.StockWarehouse).all()
    changed = 0
    for r in rows:
        new_val = str(r.warehouse_ref1c or "") in selected
        if bool(r.is_selected) != bool(new_val):
            r.is_selected = bool(new_val)
            changed += 1
    db.commit()
    return {
        "status": "ok",
        "changed": changed,
        "selected_total": sum(1 for r in rows if bool(r.is_selected)),
        "total": len(rows),
    }


@router.post("/nomenclature-odata", response_model=dict)
def sync_nomenclature_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация номенклатуры из 1С через OData.
    Перед синхронизацией номенклатуры принудительно запускается синхронизация единиц измерения,
    чтобы гарантировать целостность данных.
    Также запускается синхронизация категорий номенклатуры, чтобы привязки item->category
    попадали в БД с корректными именами групп.
    """
    try:
        # --- Шаг 1: Синхронизация единиц измерения ---
        # Создаём новый запрос для ЕИ, используя payload от номенклатуры, но меняя entity_name
        units_payload = payload.copy(deep=True)
        units_payload.entity_name = UNIT_CLASSIFIER_ENTITY
        
        # Выполняем синхронизацию ЕИ. В случае ошибки, она пробросится и остановит процесс.
        units_stats = sync_units_from_odata(db, units_payload)

        # --- Шаг 2: Синхронизация категорий номенклатуры ---
        categories_payload = payload.copy(deep=True)
        categories_payload.entity_name = "Catalog_КатегорииНоменклатуры"
        categories_payload.filter_query = None
        categories_payload.select_fields = [
            "Ref_Key",
            "Code",
            "Description",
            "Parent_Key",
            "IsFolder",
            "Predefined",
            "PredefinedDataName",
            "DataVersion",
            "DeletionMark",
        ]
        categories_stats = sync_categories_from_odata(db, categories_payload)

        # --- Шаг 3: Синхронизация номенклатуры ---
        nomenclature_stats = sync_nomenclature_from_odata(db, payload)

        # --- Шаг 4: Добивка недостающих ЕИ ---
        # После синхронизации номенклатуры могли появиться ссылки на ЕИ, которых не было
        # в основном справочнике. Добираем их.
        backfill_stats = backfill_units_from_items(db, units_payload)

        # Собираем общий результат
        return {
            "nomenclature_sync": nomenclature_stats,
            "units_sync": units_stats,
            "categories_sync": categories_stats,
            "units_backfill": backfill_stats,
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.post("/categories-odata", response_model=dict)
def sync_categories_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация категорий номенклатуры из 1С через OData.
    Тело запроса:
    {
      "base_url": "http://srv-1c:8080/base/odata/standard.odata",
      "entity_name": "Catalog_КатегорииНоменклатуры",
      "username": "user",
      "password": "pass",
      "token": null,
      "filter_query": null,
      "select_fields": null,
      "dry_run": false,
      "zero_missing": false
    }
    """
    try:
        stats = sync_categories_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.post("/specifications-odata", response_model=dict)
def sync_specifications_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация спецификаций из 1С через OData.
    Тело запроса:
    {
      "base_url": "http://srv-1c:8080/base/odata/standard.odata",
      "entity_name": "Catalog_Спецификации",
      "username": "user",
      "password": "pass",
      "token": null,
      "filter_query": null,
      "select_fields": null,
      "dry_run": false,
      "zero_missing": false
    }
    """
    try:
        stats = sync_specifications_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.post("/production-orders-odata", response_model=dict)
def sync_production_orders_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация заказов на производство из 1С через OData.
    Тело запроса:
    {
      "base_url": "http://srv-1c:8080/base/odata/standard.odata",
      "entity_name": "Document_ЗаказНаПроизводство",
      "username": "user",
      "password": "pass",
      "token": null,
      "filter_query": null,
      "select_fields": null,
      "dry_run": false,
      "zero_missing": false
    }
    """
    try:
        stats = sync_production_orders_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.get("/production-orders-odata/export", response_model=dict)
def export_production_orders(db: Session = Depends(get_db)):
    """
    Экспорт заказов на производство в Excel (XLSX, base64).
    Данные берутся из БД (production_orders + production_products + items).

    Возвращает:
    {
      "status": "ok",
      "format": "xlsx",
      "data_base64": "<base64 encoded file>",
      "filename": "production_orders_20260219_120000.xlsx",
      "total_rows": 150,
      "orders_count": 25
    }
    """
    try:
        result = export_production_orders_xlsx(db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export error: {e}")


@router.post("/production-orders-fact-odata", response_model=dict)
def sync_production_orders_fact_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация факта выпуска из 1С через OData.
    Загружает данные из Document_СборкаЗапасов и обновляет produced_qty/remaining_qty.
    
    Тело запроса (как для ODataSyncRequest):
    {
      "base_url": "http://srv-1c:8080/base/odata/standard.odata",
      "entity_name": "Document_СборкаЗапасов",
      "username": "user",
      "password": "pass",
      "token": null,
      "filter_query": null,
      "select_fields": null,
      "dry_run": false,
      "zero_missing": false
    }
    """
    try:
        stats = sync_production_fact_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.get("/debug/production-order-states", response_model=dict)
def debug_production_order_states(db: Session = Depends(get_db)):
    """
    Отладка: получение всех уникальных состояний заказов из 1С.
    """
    from ..services.odata_client import OData1CClient
    from ..services.odata_config import load_odata_config, sanitize_base_url

    # Получаем конфиг OData из файла (в этом проекте он хранится не в БД)
    config = load_odata_config()
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        raise HTTPException(status_code=404, detail="OData config not found")

    client = OData1CClient(
        base_url=sanitize_base_url(base_url),
        username=config.get("username") or None,
        password=config.get("password") or None,
        token=config.get("token") or None,
    )
    
    # Загружаем заказы с состояниями
    data = client.get_all(
        'Document_ЗаказНаПроизводство',
        select_fields=['Ref_Key', 'Number', 'СостояниеЗаказа_Key', 'DeletionMark'],
        top=1000,
        max_pages=5
    )
    
    states = {}
    deleted_count = 0
    for rec in data:
        dm = rec.get('DeletionMark', False)
        if dm is True or dm == "true":
            deleted_count += 1
            continue
        key = str(rec.get('СостояниеЗаказа_Key', '') or '').strip()
        if key and key not in states:
            states[key] = rec.get('Number', '')
    
    return {
        "total_loaded": len(data),
        "deleted_count": deleted_count,
        "active_states": states,
        "sample_order": data[0] if data else None
    }


@router.post("/supplier-orders-odata", response_model=dict)
def sync_supplier_orders_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация заказов поставщикам из 1С через OData.
    Тело запроса:
    {
      "base_url": "http://srv-1c:8080/base/odata/standard.odata",
      "entity_name": "Document_ЗаказПоставщику",
      "username": "user",
      "password": "pass",
      "token": null,
      "filter_query": null,
      "select_fields": null,
      "dry_run": false,
      "zero_missing": false
    }
    """
    try:
        stats = sync_supplier_orders_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.get("/supplier-orders-odata/export", response_model=dict)
def export_supplier_orders(db: Session = Depends(get_db)):
    """
    Экспорт учитываемых заказов поставщику в Excel (XLSX, base64).
    """
    try:
        result = export_supplier_orders_xlsx(db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export error: {e}")


@router.post("/default-specifications-odata", response_model=dict)
def sync_default_specifications_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация спецификаций по умолчанию из 1С через OData.
    Тело запроса:
    {
      "base_url": "http://srv-1c:8080/base/odata/standard.odata",
      "entity_name": "InformationRegister_СпецификацииПоУмолчанию",
      "username": "user",
      "password": "pass",
      "token": null,
      "filter_query": null,
      "select_fields": null,
      "dry_run": false,
      "zero_missing": false
    }
    """
    try:
        stats = sync_default_specifications_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.post("/production-stages-odata", response_model=dict)
def sync_production_stages_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация этапов производства из 1С через OData.
    Ожидаемая сущность: каталог этапов (например, "Catalog_ЭтапыПроизводства").
    """
    try:
        stats = sync_production_stages_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")



@router.post("/units-odata", response_model=dict)
def sync_units_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация единиц измерения из 1С через OData.
    Ожидаемая сущность: "Catalog_КлассификаторЕдиницИзмерения" (или аналог).
    Дополнительно выполняется добивка недостающих ЕИ по GUID из items.unit.
    """
    try:
        if payload.entity_name == UNIT_ENTITY:
            payload = payload.copy(deep=True)
            payload.entity_name = UNIT_CLASSIFIER_ENTITY
        stats = sync_units_from_odata(db, payload)
        # Пытаемся добрать недостающие GUID из items.unit из альтернативных каталогов
        try:
            backfill = backfill_units_from_items(db, payload)
            if isinstance(stats, dict):
                stats["backfill"] = backfill
        except Exception as be:
            # Не валим общий результат синхронизации, просто добавим информацию об ошибке добивки
            if not isinstance(stats, dict):
                stats = {"stats": stats}
            stats["backfill_error"] = str(be)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")

@router.post("/operations-odata", response_model=dict)
def sync_operations_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация наименований операций через строки спецификаций 1С.
    По умолчанию используем сущность "Catalog_Спецификации_Операции" и навигацию Операция@navigationLinkUrl.
    """
    try:
        # Фоллбэк сущности, если не указана
        if not getattr(payload, "entity_name", None):
            payload.entity_name = "Catalog_Спецификации_Операции"
        stats = sync_operations_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.post("/employees-odata", response_model=dict)
def sync_employees_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация сотрудников из 1С через OData.
    По умолчанию используем сущность "Catalog_Сотрудники".
    """
    try:
        if not getattr(payload, "entity_name", None):
            payload.entity_name = "Catalog_Сотрудники"
        stats = sync_employees_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.post("/production-kinds-odata", response_model=dict)
def sync_production_kinds_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация видов производства из 1С через OData.
    Ожидаемая сущность: "Catalog_ВидыПроизводства" (или аналог).
    """
    try:
        stats = sync_production_kinds_from_odata(db, payload)
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {e}")


@router.post("/auto/tick", response_model=dict)
def sync_auto_tick(db: Session = Depends(get_db)):
    """
    Выполнить не более одного «просроченного» job автоматической синхронизации.
    Вызывается воркером каждые ~2 минуты: один job за тик → нагрузка на 1С
    размазана по времени, без пиков и параллельных запусков. Read-only к 1С.
    """
    try:
        return sync_orchestrator.tick(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync tick error: {e}")


@router.get("/auto/status", response_model=dict)
def sync_auto_status(db: Session = Depends(get_db)):
    """Состояние расписания авто-синхронизации: по каждому job — последний запуск,
    интервал, когда следующий, статус/ошибка."""
    try:
        return sync_orchestrator.status(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync status error: {e}")


@router.post("/auto/config", response_model=dict)
def sync_auto_config(payload: SyncAutoConfigPayload):
    """Правка расписания: {jobs: {<job_id>: {interval_seconds?, enabled?}}}.
    Возвращает обновлённое состояние."""
    try:
        updates = {
            job_id: patch.dict(exclude_unset=True)
            for job_id, patch in (payload.jobs or {}).items()
        }
        return sync_orchestrator.update_config(updates)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync config error: {e}")


@router.get("/progress")
def get_sync_progress(key: str = "nomenclature"):
    """
    Текущее состояние прогресса длительных операций синхронизации.
    Пример: GET /api/v1/sync/progress?key=nomenclature
    Возвращает:
    {
      "total": int,
      "processed": int,
      "percent": float (0..1),
      "finished": bool,
      "error": Optional[str],
      "message": str
    }
    """
    try:
        from ..services.progress_manager import progress  # lazy import чтобы не держать цикл
        state = progress.get_state(key)
        # Базовая валидация полей
        state.setdefault("total", 0)
        state.setdefault("processed", 0)
        p = state.get("percent") or 0.0
        state["percent"] = max(0.0, min(1.0, float(p)))
        state.setdefault("finished", False)
        state.setdefault("error", None)
        state.setdefault("message", "")
        return state
    except Exception as e:
        # Не позволяем падать — возвращаем дефолтное состояние
        return {
            "total": 0,
            "processed": 0,
            "percent": 0.0,
            "finished": False,
            "error": f"{e}",
            "message": ""
        }
