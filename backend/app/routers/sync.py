from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import ODataSyncRequest, ODataSyncStats
from ..services.odata_stock_sync import sync_stock_from_odata
from ..services.nomenclature_sync import sync_nomenclature_from_odata, NomenclatureSyncStats
from ..services.category_sync import sync_categories_from_odata, CategorySyncStats
from ..services.specification_sync import sync_specifications_from_odata, SpecificationSyncStats
from ..services.production_order_sync import sync_production_orders_from_odata, ProductionOrderSyncStats
from ..services.supplier_order_sync import sync_supplier_orders_from_odata, SupplierOrderSyncStats
from ..services.default_specification_sync import sync_default_specifications_from_odata, DefaultSpecificationSyncStats
from ..services.production_stage_sync import sync_production_stages_from_odata, ProductionStageSyncStats

from ..services.units_sync import sync_units_from_odata, backfill_units_from_items
from ..services.operations_sync import sync_operations_from_odata, OperationsSyncStats

router = APIRouter(prefix="/v1/sync", tags=["sync"])


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


@router.post("/nomenclature-odata", response_model=dict)
def sync_nomenclature_odata(payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Синхронизация номенклатуры из 1С через OData.
    Тело запроса:
    {
      "base_url": "http://srv-1c:8080/base/odata/standard.odata",
      "entity_name": "Catalog_Номенклатура",
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
        stats = sync_nomenclature_from_odata(db, payload)
        return stats
    except Exception as e:
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
    Ожидаемая сущность: "Catalog_ЕдиницыИзмерения" (или аналог).
    Дополнительно выполняется добивка недостающих ЕИ по GUID из items.unit.
    """
    try:
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