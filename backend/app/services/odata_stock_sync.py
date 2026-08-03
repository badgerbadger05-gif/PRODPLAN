from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .odata_client import OData1CClient
from .odata_client import get_stock_from_1c_odata
from ..models import Item, StockWarehouse
from ..schemas import ODataSyncRequest


@dataclass
class _Stats:
    items_total: int = 0
    matched_in_odata: int = 0
    unmatched_zeroed: int = 0
    items_updated: int = 0
    items_unchanged: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""
    warehouses_total: int = 0
    warehouses_selected: int = 0
    # per-warehouse breakdown mirrors were removed in favor of accepted Ledger bins


@dataclass
class _WarehouseStats:
    warehouses_seen_in_odata: int = 0
    warehouses_changed: int = 0
    warehouses_total: int = 0
    warehouses_selected: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def _norm_code(s: str) -> str:
    """
    Нормализация кодов для устойчивого сопоставления:
    - trim + upper
    - убрать все не буквенно-цифровые символы (дефисы, пробелы, слеши и т.п.)
    - если это целое число ('1234' или '001234' или '1234.0') — привести к '1234' без ведущих нулей
    """
    import re

    t = str(s or "").strip().upper()
    if not t:
        return ""
    # Удаляем все небуквенно-цифровые символы
    t = re.sub(r"[^0-9A-Z]+", "", t)

    # Если после очистки остались только цифры — убираем ведущие нули
    if re.fullmatch(r"\d+", t):
        t = t.lstrip("0") or "0"
    return t


def _fetch_db_code_maps(db: Session) -> Dict[str, str]:
    """
    Вернуть отображение:
      raw_code -> normalized_code
    Также формируем обратную карту при использовании (через построение set/lookup по норм-коду).
    """
    result: Dict[str, str] = {}
    for it in db.query(Item).with_entities(Item.item_code).all():
        raw = str(it[0]).strip()
        result[raw] = _norm_code(raw)
    return result


def _upsert_warehouses_from_stock_rows(db: Session, stock_rows: List[Dict]) -> int:
    """
    Upsert складов из строк регистра остатков.
    Новые склады по умолчанию выбираются (is_selected=true), чтобы сохранить текущее поведение "учитывать всё".
    """
    # Готовим уникальные склады по Ref_Key
    by_ref: Dict[str, Dict[str, str]] = {}
    for rec in stock_rows or []:
        w_ref = str(rec.get("warehouse_ref") or "").strip()
        if not w_ref:
            continue
        if w_ref not in by_ref:
            by_ref[w_ref] = {
                "warehouse_code": str(rec.get("warehouse_code") or "").strip(),
                "warehouse_name": str(rec.get("warehouse_name") or "").strip() or w_ref,
            }
        else:
            if not by_ref[w_ref].get("warehouse_name"):
                by_ref[w_ref]["warehouse_name"] = str(rec.get("warehouse_name") or "").strip() or w_ref
            if not by_ref[w_ref].get("warehouse_code"):
                by_ref[w_ref]["warehouse_code"] = str(rec.get("warehouse_code") or "").strip()

    if not by_ref:
        return 0

    existing_rows: List[StockWarehouse] = (
        db.query(StockWarehouse)
        .filter(StockWarehouse.warehouse_ref1c.in_(list(by_ref.keys())))
        .all()
    )
    existing_by_ref = {str(x.warehouse_ref1c): x for x in existing_rows}

    changed = 0
    for w_ref, payload in by_ref.items():
        row = existing_by_ref.get(w_ref)
        if row is None:
            db.add(
                StockWarehouse(
                    warehouse_ref1c=w_ref,
                    warehouse_code=payload.get("warehouse_code") or None,
                    warehouse_name=payload.get("warehouse_name") or w_ref,
                    is_selected=True,
                )
            )
            changed += 1
            continue

        code_new = payload.get("warehouse_code") or None
        name_new = payload.get("warehouse_name") or w_ref
        needs_update = False
        if str(row.warehouse_name or "") != str(name_new):
            row.warehouse_name = name_new
            needs_update = True
        if str(row.warehouse_code or "") != str(code_new or ""):
            row.warehouse_code = code_new
            needs_update = True
        if needs_update:
            changed += 1

    return changed


def _upsert_warehouses_from_catalog_rows(db: Session, rows: List[Dict]) -> int:
    by_ref: Dict[str, Dict[str, str]] = {}
    for rec in rows or []:
        if rec.get("DeletionMark") is True or str(rec.get("DeletionMark") or "").lower() == "true":
            continue
        w_ref = str(rec.get("Ref_Key") or rec.get("RefKey") or "").strip()
        if not w_ref:
            continue
        by_ref[w_ref] = {
            "warehouse_code": str(rec.get("Code") or rec.get("Код") or "").strip(),
            "warehouse_name": str(
                rec.get("Description")
                or rec.get("Наименование")
                or rec.get("Name")
                or w_ref
            ).strip(),
        }

    if not by_ref:
        return 0

    existing_rows: List[StockWarehouse] = (
        db.query(StockWarehouse)
        .filter(StockWarehouse.warehouse_ref1c.in_(list(by_ref.keys())))
        .all()
    )
    existing_by_ref = {str(x.warehouse_ref1c): x for x in existing_rows}

    changed = 0
    for w_ref, payload in by_ref.items():
        row = existing_by_ref.get(w_ref)
        if row is None:
            db.add(
                StockWarehouse(
                    warehouse_ref1c=w_ref,
                    warehouse_code=payload.get("warehouse_code") or None,
                    warehouse_name=payload.get("warehouse_name") or w_ref,
                    is_selected=True,
                )
            )
            changed += 1
            continue

        code_new = payload.get("warehouse_code") or None
        name_new = payload.get("warehouse_name") or w_ref
        needs_update = False
        if str(row.warehouse_name or "") != str(name_new):
            row.warehouse_name = name_new
            needs_update = True
        if str(row.warehouse_code or "") != str(code_new or ""):
            row.warehouse_code = code_new
            needs_update = True
        if needs_update:
            changed += 1

    return changed


def _fetch_warehouse_catalog_rows(req: ODataSyncRequest) -> Tuple[List[Dict], str]:
    client = OData1CClient(req.base_url, req.username, req.password, req.token)
    candidate_entities = [
        "Catalog_Склады",
        "Catalog_СтруктурныеЕдиницы",
        "Catalog_СтруктурныеЕдиницыПредприятия",
        "Catalog_СкладыПредприятия",
    ]
    last_error: Optional[Exception] = None
    rows_by_ref: Dict[str, Dict] = {}
    used_entities: List[str] = []
    for entity in candidate_entities:
        try:
            rows = client.get_all(
                entity_name=entity,
                select_fields=["Ref_Key", "Code", "Description", "DeletionMark"],
                top=1000,
                max_pages=100,
                order_by="Ref_Key",
            )
        except Exception as exc:
            last_error = exc
            continue
        if rows:
            used_entities.append(entity)
        for rec in rows or []:
            w_ref = str(rec.get("Ref_Key") or rec.get("RefKey") or "").strip()
            if not w_ref:
                continue
            existing = rows_by_ref.get(w_ref)
            if existing is None:
                rows_by_ref[w_ref] = rec
                continue
            if not str(existing.get("Description") or existing.get("Наименование") or existing.get("Name") or "").strip():
                rows_by_ref[w_ref] = rec
    if rows_by_ref:
        return list(rows_by_ref.values()), ", ".join(used_entities)
    if last_error:
        print(f"[OData][warehouses] catalog lookup fallback: {last_error}", flush=True)
    return [], ""


def sync_stock_from_odata(
    db: Session,
    req: ODataSyncRequest,
) -> dict:
    """
    Read-only OData stock diagnostic plus warehouse-catalog refresh.

    Physical quantity is published exclusively by the Item Ledger physical
    refresh lifecycle. This compatibility endpoint never stores an aggregate
    quantity on ``Item``.
    """
    stats = _Stats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    # Прогресс-бар (опционально)
    try:
        from ..services.progress_manager import progress  # type: ignore
    except Exception:
        progress = None  # type: ignore

    # Карта кодов из БД
    db_code_to_norm = _fetch_db_code_maps(db)
    stats.items_total = len(db_code_to_norm)

    # Прогресс: старт до запроса к 1С (total пока неизвестен)
    if progress:
        try:
            progress.start("stock", total=0, message="Загрузка остатков из 1С")
        except Exception:
            pass

    # Получение данных из OData
    stock_data = get_stock_from_1c_odata(
        base_url=req.base_url,
        entity_name=req.entity_name,
        username=req.username,
        password=req.password,
        token=req.token,
        filter_query=req.filter_query,
        select_fields=req.select_fields,
    )

    if progress:
        try:
            progress.update("stock", message=f"Получено от 1С: {len(stock_data)} записей")
        except Exception:
            pass

    # Если 1С ничего не вернула — не обновляем, чтобы не обнулять случайно
    if not stock_data:
        stats.dry_run = True
        if progress:
            try:
                progress.finish("stock", error=None, message="Нет данных от 1С, dry-run")
            except Exception:
                pass
        return asdict(stats)

    # Full (pre-warehouse-filter) Balance snapshot for the  reconcile
    # after-step: bins in any known warehouse must reconcile against 1С
    # regardless of the planning-selection contour, so capture before the
    # selected-warehouse filter reassigns stock_data below.
    full_balance_rows = list(stock_data)

    # 1) Обновляем справочник складов из ответа 1С
    _upsert_warehouses_from_stock_rows(db, stock_data)
    db.flush()

    # 2) Применяем фильтр по выбранным складам (мультивыбор из UI)
    warehouses_total = int(db.query(StockWarehouse).count() or 0)
    selected_refs_rows = (
        db.query(StockWarehouse.warehouse_ref1c)
        .filter(StockWarehouse.is_selected.is_(True))
        .all()
    )
    selected_refs = {str(x[0]).strip() for x in selected_refs_rows if x and x[0]}
    stats.warehouses_total = warehouses_total
    stats.warehouses_selected = len(selected_refs)

    if warehouses_total > 0:
        filtered_rows: List[Dict] = []
        for rec in stock_data:
            w_ref = str(rec.get("warehouse_ref") or "").strip()
            # Если склад в строке не определён — оставляем строку (иначе риск ложного обнуления)
            if not w_ref:
                filtered_rows.append(rec)
                continue
            if w_ref in selected_refs:
                filtered_rows.append(rec)
        stock_data = filtered_rows

    # 3) Aggregate only for diagnostic matching. Physical publication belongs
    #    to the accepted Item Ledger generation.

    # Агрегируем по нормализованным кодам И по Ref_Key (GUID) — GUID имеет приоритет для сопоставления
    odata_map_norm_to_qty: Dict[str, float] = {}
    odata_map_ref_to_qty: Dict[str, float] = {}
    for rec in stock_data:
        qty = float(rec.get("qty") or 0.0)

        # По коду
        norm = _norm_code(rec.get("code", ""))
        if norm:
            odata_map_norm_to_qty[norm] = odata_map_norm_to_qty.get(norm, 0.0) + qty

        # По GUID
        ref = str(rec.get("ref") or "").strip()
        if ref:
            odata_map_ref_to_qty[ref] = odata_map_ref_to_qty.get(ref, 0.0) + qty

    # Debug: показать часть "сырых" кодов/Ref до нормализации
    try:
        sample_raw_codes = [rec.get("code") for rec in stock_data[:10]]
        sample_raw_refs = [rec.get("ref") for rec in stock_data[:10]]
        print(f"[OData][stock] sample raw codes: {sample_raw_codes}", flush=True)
        print(f"[OData][stock] sample raw refs: {sample_raw_refs}", flush=True)
    except Exception:
        pass

    # Debug: вывести примеры норм-кодов/Ref для диагностики сопоставления
    try:
        sample_db_codes = list({v for v in db_code_to_norm.values()})[:10]
        sample_odata_codes = list(odata_map_norm_to_qty.keys())[:10]
        sample_odata_refs = list(odata_map_ref_to_qty.keys())[:10]
        print(f"[OData][stock] DB codes total={len(db_code_to_norm)} sample={sample_db_codes}", flush=True)
        print(f"[OData][stock] OData codes total={len(odata_map_norm_to_qty)} sample={sample_odata_codes}", flush=True)
        print(f"[OData][stock] OData refs total={len(odata_map_ref_to_qty)} sample={sample_odata_refs}", flush=True)
    except Exception:
        pass

    # Подсчёт совпавших по GUID или по нормализованному коду
    # Пройдёмся по всем items в БД и посчитаем, сколько найдётся либо по ref, либо по коду
    matched = 0
    try:
        items_for_match: List[Item] = db.query(Item).with_entities(Item.item_code, Item.item_ref1c).all()
        for row in items_for_match:
            raw_code = str(row[0] or "").strip()
            ref1c = str(row[1] or "").strip()
            norm_code = db_code_to_norm.get(raw_code, _norm_code(raw_code))
            if (ref1c and ref1c in odata_map_ref_to_qty) or (norm_code and norm_code in odata_map_norm_to_qty):
                matched += 1
    except Exception:
        matched = sum(1 for norm in db_code_to_norm.values() if norm in odata_map_norm_to_qty)
    stats.matched_in_odata = matched

    stats.items_unchanged = stats.items_total
    if req.dry_run:
        db.rollback()
    else:
        db.commit()
    if progress:
        try:
            progress.finish(
                "stock",
                error=None,
                message="Остатки проверены; публикация выполняется Item Ledger",
            )
        except Exception:
            pass

    return asdict(stats)


def sync_stock_warehouses_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизирует только справочник складов из регистра остатков 1С.
    Не публикует физические остатки.
    """
    stats = _WarehouseStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    catalog_rows, catalog_entity = _fetch_warehouse_catalog_rows(req)
    if catalog_rows:
        stats.odata_entity = catalog_entity
        stats.warehouses_seen_in_odata = len(
            {
                str(rec.get("Ref_Key") or rec.get("RefKey") or "").strip()
                for rec in catalog_rows
                if str(rec.get("Ref_Key") or rec.get("RefKey") or "").strip()
            }
        )
        stats.warehouses_changed = _upsert_warehouses_from_catalog_rows(db, catalog_rows)
        db.flush()
        stats.warehouses_total = int(db.query(StockWarehouse).count() or 0)
        stats.warehouses_selected = int(
            db.query(StockWarehouse).filter(StockWarehouse.is_selected.is_(True)).count() or 0
        )
        if req.dry_run:
            db.rollback()
        else:
            db.commit()
        return asdict(stats)

    stock_data = get_stock_from_1c_odata(
        base_url=req.base_url,
        entity_name=req.entity_name,
        username=req.username,
        password=req.password,
        token=req.token,
        filter_query=req.filter_query,
        select_fields=req.select_fields,
    )
    if not stock_data:
        return asdict(stats)

    seen_refs = {
        str(rec.get("warehouse_ref") or "").strip()
        for rec in stock_data
        if str(rec.get("warehouse_ref") or "").strip()
    }
    stats.warehouses_seen_in_odata = len(seen_refs)
    stats.warehouses_changed = _upsert_warehouses_from_stock_rows(db, stock_data)
    db.flush()
    stats.warehouses_total = int(db.query(StockWarehouse).count() or 0)
    stats.warehouses_selected = int(
        db.query(StockWarehouse).filter(StockWarehouse.is_selected.is_(True)).count() or 0
    )

    if req.dry_run:
        db.rollback()
    else:
        db.commit()

    return asdict(stats)
