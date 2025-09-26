from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List

from sqlalchemy.orm import Session

from .odata_client import get_stock_from_1c_odata
from ..models import Item
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


def sync_stock_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация остатков из 1С через OData.
    Алгоритм аналогичен PRODPLANOLD/src/odata_stock_sync.py:
      - чтение всех item_code из БД и нормализация
      - загрузка остатков из 1С и агрегация по нормализованным кодам
      - безопасное обновление stock_qty в одной транзакции
      - флаги dry_run / zero_missing
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

    if not odata_map_norm_to_qty and not odata_map_ref_to_qty:
        stats.dry_run = True
        if progress:
            try:
                progress.finish("stock", error=None, message="Пустая карта остатков, dry-run")
            except Exception:
                pass
        # Debug: показать первые поля записи при пустом результате
        try:
            if stock_data:
                sample = stock_data[0]
                print("[OData][stock] Empty map, sample fields:", list(sample.keys())[:20], flush=True)
        except Exception:
            pass
        return asdict(stats)

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

    zeroed_count = 0
    updated = 0
    unchanged = 0

    # Обновим все записи items
    items: List[Item] = db.query(Item).all()
    total = len(items)

    # Инициализация прогресса перед долгой секцией обновления БД (обновим total)
    if progress:
        try:
            progress.update("stock", total=total or 0, processed=0, message="Обновление остатков в БД")
        except Exception:
            pass

    processed = 0
    try:
        for it in items:
            raw_code = str(it.item_code or "").strip()
            ref1c = str(it.item_ref1c or "").strip()
            norm_code = db_code_to_norm.get(raw_code, _norm_code(raw_code))
            old_qty = float(it.stock_qty or 0.0)

            # Приоритет сопоставления:
            # 1) по GUID (item_ref1c)
            # 2) по нормализованному коду
            if ref1c and ref1c in odata_map_ref_to_qty:
                new_qty = float(odata_map_ref_to_qty[ref1c])
            elif norm_code in odata_map_norm_to_qty:
                new_qty = float(odata_map_norm_to_qty[norm_code])
            else:
                if req.zero_missing:
                    new_qty = 0.0
                else:
                    new_qty = old_qty

            if abs(old_qty - new_qty) > 1e-9:
                if (norm_code not in odata_map_norm_to_qty) and req.zero_missing and old_qty != 0.0:
                    zeroed_count += 1
                it.stock_qty = new_qty
                updated += 1
            else:
                unchanged += 1

            processed += 1
            if progress:
                try:
                    # Обновляем чаще для малых объёмов
                    if total and total <= 50:
                        do_update = True
                    else:
                        update_every = 10 if (total and total > 0) else 50
                        do_update = (processed % update_every == 0)
                    if do_update or processed == total:
                        msg = f"Обработано {processed}" + (f"/{total}" if total > 0 else "")
                        progress.update("stock", processed=processed, message=msg)
                except Exception:
                    pass

        stats.unmatched_zeroed = zeroed_count
        stats.items_updated = updated
        stats.items_unchanged = unchanged

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

        if progress:
            try:
                done = processed or total
                progress.update("stock", processed=done, message=f"Готово: {done}/{total or done}")
                progress.finish("stock", error=None, message="Синхронизация остатков завершена")
            except Exception:
                pass
    except Exception as e:
        db.rollback()
        if progress:
            try:
                progress.finish("stock", error=str(e), message="Синхронизация остатков завершилась ошибкой")
            except Exception:
                pass
        raise

    return asdict(stats)