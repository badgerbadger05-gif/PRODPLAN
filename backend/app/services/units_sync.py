from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ..schemas import ODataSyncRequest
from ..models import Unit, Item
from ..services.odata_client import OData1CClient


def _to_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return float(default)
        if isinstance(val, (int, float)):
            return float(val)
        return float(Decimal(str(val)))
    except (InvalidOperation, ValueError, TypeError):
        return float(default)


def _to_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if val is None or val == "":
            return default
        return int(val)
    except Exception:
        try:
            return int(float(val))
        except Exception:
            return default


def _s(val: Any) -> str:
    return str(val or "").strip()


@dataclass
class UnitsSyncStats:
    """Статистика синхронизации единиц измерения"""
    units_total: int = 0
    units_created: int = 0
    units_updated: int = 0
    units_unchanged: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def sync_units_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация справочника единиц измерения из 1С через OData.

    Предполагаемая сущность: Catalog_ЕдиницыИзмерения (или аналог в конфигурации).
    Мэппинг полей гибкий: берём безопасные Ref_Key, Code, Description, а расширенные атрибуты пытаемся
    читать по частым названиям, если они присутствуют в конфигурации.

    Алгоритм:
      1) Постраничная выборка из OData (без $select, чтобы не упасть на вариантах метаданных).
      2) upsert по unit_ref1c (Ref_Key). При его отсутствии пробуем по unit_code.
      3) Обновляем/создаём записи Unit.
      4) Ведём прогресс через progress_manager с ключом "units".
    """
    client = OData1CClient(req.base_url, req.username, req.password, req.token)

    # Инициализируем статистику
    stats = UnitsSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    # Прогресс
    try:
        from ..services.progress_manager import progress
    except Exception:
        progress = None  # type: ignore

    # Пытаемся получить общий объём
    total_count = 0
    try:
        total_count = client.get_count(req.entity_name, None)
    except Exception:
        total_count = 0

    if progress:
        progress.start("units", total=total_count or 0, message="Загрузка единиц измерения из 1С")

    if total_count > 0:
        stats.units_total = int(total_count)

    # Текущие записи в БД (кэш по guid/code)
    existing_by_ref = {u.unit_ref1c: u for u in db.query(Unit).all() if u.unit_ref1c}
    existing_by_code = {u.unit_code: u for u in db.query(Unit).all() if u.unit_code}

    created = 0
    updated = 0
    unchanged = 0
    processed = 0

    try:
        # Выбираем страницы; без $select, чтобы не падать на кастомных конфигурациях
        for page in client.iter_pages(
            req.entity_name,
            filter_query=None,
            select_fields=None,  # тянем все доступные поля для гибкости
            top=1000,
            max_pages=1000,
            order_by="Ref_Key",
        ):
            for r in page:
                try:
                    ref_key = _s(r.get("Ref_Key"))
                    if not ref_key:
                        # Без Ref_Key смысла нет — пропустим
                        continue

                    processed += 1
                    if progress and (processed % 20 == 0):
                        msg = f"Обработано {processed}" + (f"/{total_count}" if total_count > 0 else "")
                        progress.update("units", processed=processed, message=msg)

                    code = _s(r.get("Code")) or None
                    name = _s(r.get("Description")) or None

                    # Расширенные поля (опциональные и могут отсутствовать)
                    full_name = _s(r.get("НаименованиеПолное") or r.get("ПолноеНаименование") or r.get("FullName") or "")
                    short_name = _s(r.get("Сокращение") or r.get("КраткоеНаименование") or r.get("ShortName") or "")
                    iso_code = _s(
                        r.get("МеждународноеСокращение")
                        or r.get("МеждународноеОбозначение")
                        or r.get("ISOCode")
                        or r.get("МеждународноеНаименованиеКраткое")
                        or ""
                    )
                    base_unit_ref1c = _s(
                        r.get("БазоваяЕдиница_Key")
                        or r.get("БазоваяЕдиницаИзмерения_Key")
                        or r.get("BaseUnit_Key")
                        or ""
                    ) or None
                    ratio = _to_float(r.get("Кратность") or r.get("Коэффициент") or 1.0, 1.0)
                    precision = _to_int(r.get("Точность"), None)

                    # Минимальные фоллбэки
                    if not name:
                        name = code or ref_key

                    existing = existing_by_ref.get(ref_key)
                    if existing:
                        # Проверим, есть ли изменения
                        need_update = False

                        if existing.unit_code != code:
                            existing.unit_code = code
                            need_update = True
                        if existing.unit_name != name:
                            existing.unit_name = name
                            need_update = True
                        if (existing.unit_full_name or "") != (full_name or None):
                            existing.unit_full_name = full_name or None
                            need_update = True
                        if (existing.short_name or "") != (short_name or None):
                            existing.short_name = short_name or None
                            need_update = True
                        if (existing.iso_code or "") != (iso_code or None):
                            existing.iso_code = iso_code or None
                            need_update = True
                        if (existing.base_unit_ref1c or "") != (base_unit_ref1c or None):
                            existing.base_unit_ref1c = base_unit_ref1c or None
                            need_update = True
                        if (existing.ratio or 1.0) != ratio:
                            existing.ratio = ratio
                            need_update = True
                        if (existing.precision or None) != precision:
                            existing.precision = precision
                            need_update = True

                        if need_update:
                            updated += 1
                        else:
                            unchanged += 1
                    else:
                        # Попробуем найти по коду (если Ref_Key новый, но код совпадает)
                        if code and code in existing_by_code:
                            u = existing_by_code[code]
                            u.unit_ref1c = ref_key
                            u.unit_name = name
                            u.unit_full_name = full_name or None
                            u.short_name = short_name or None
                            u.iso_code = iso_code or None
                            u.base_unit_ref1c = base_unit_ref1c or None
                            u.ratio = ratio
                            u.precision = precision
                            existing_by_ref[ref_key] = u
                            updated += 1
                        else:
                            # Создаём новую единицу
                            u = Unit(
                                unit_ref1c=ref_key,
                                unit_code=code,
                                unit_name=name or "",
                                unit_full_name=full_name or None,
                                short_name=short_name or None,
                                iso_code=iso_code or None,
                                base_unit_ref1c=base_unit_ref1c or None,
                                ratio=ratio,
                                precision=precision,
                            )
                            db.add(u)
                            existing_by_ref[ref_key] = u
                            if code:
                                existing_by_code[code] = u
                            created += 1

                    # Периодический сброс хвоста
                    if (processed % 1000) == 0:
                        db.flush()

                except Exception as rec_e:
                    # Логируем и продолжаем
                    print(f"Ошибка обработки записи единицы измерения: {rec_e}")
                    continue

        # Если total неизвестен — выставим по факту
        if stats.units_total == 0:
            stats.units_total = int(processed)

        # Финальный апдейт прогресса
        if progress:
            progress.update("units", processed=processed, message=f"Готово: {processed}/{stats.units_total or processed}")
            progress.finish("units", error=None, message="Синхронизация единиц завершена")

        stats.units_created = created
        stats.units_updated = updated
        stats.units_unchanged = unchanged

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

        return asdict(stats)

    except Exception as e:
        db.rollback()
        # Закрываем прогресс с ошибкой
        if progress:
            try:
                progress.finish("units", error=str(e), message="Синхронизация единиц завершилась ошибкой")
            except Exception:
                pass
        raise Exception(f"Ошибка синхронизации единиц измерения: {e}")



# --- Дополнительная процедура: добивка ЕИ по GUID из items.unit (если они отсутствуют в таблице units)
from typing import List

def backfill_units_from_items(db: Session, req: ODataSyncRequest, catalogs: Optional[List[str]] = None) -> dict:
    """
    Добирает записи единиц измерения по GUID из Item.unit, которых нет в таблице units.
    Пробует найти их в одном или нескольких справочниках 1С (по умолчанию:
    'Catalog_ЕдиницыИзмерения' и 'Catalog_КлассификаторЕдиницИзмерения').
    """
    client = OData1CClient(req.base_url, req.username, req.password, req.token)

    # Соберём уникальные GUID из items.unit
    item_unit_guids = {str(t[0]).strip() for t in db.query(Item.unit).filter(Item.unit.isnot(None)).all() if t and t[0]}
    # Существующие GUID в units
    existing_guids = {str(u.unit_ref1c).strip() for u in db.query(Unit).all() if u.unit_ref1c}
    missing = {g for g in item_unit_guids if g and g not in existing_guids}

    result = {
        "missing_before": len(missing),
        "found": 0,
        "created": 0,
        "updated": 0,
        "missing_after": None,  # заполним позже
        "catalogs": catalogs or ["Catalog_ЕдиницыИзмерения", "Catalog_КлассификаторЕдиницИзмерения"],
    }

    if not missing:
        result["missing_after"] = 0
        return result

    catalogs_to_probe = catalogs or ["Catalog_ЕдиницыИзмерения", "Catalog_КлассификаторЕдиницИзмерения"]

    created = 0
    updated = 0
    found_keys: set[str] = set()

    def upsert_from_row(row: Dict[str, Any]) -> None:
        nonlocal created, updated, found_keys
        ref_key = _s(row.get("Ref_Key"))
        if not ref_key:
            return
        if ref_key not in missing:
            return

        code = _s(row.get("Code")) or None
        name = _s(row.get("Description")) or None
        full_name = _s(row.get("НаименованиеПолное") or row.get("ПолноеНаименование") or row.get("FullName") or "")
        short_name = _s(row.get("Сокращение") or row.get("КраткоеНаименование") or row.get("ShortName") or "")
        iso_code = _s(
            row.get("МеждународноеСокращение")
            or row.get("МеждународноеОбозначение")
            or row.get("ISOCode")
            or row.get("МеждународноеНаименованиеКраткое")
            or ""
        )
        base_unit_ref1c = _s(
            row.get("БазоваяЕдиница_Key")
            or row.get("БазоваяЕдиницаИзмерения_Key")
            or row.get("BaseUnit_Key")
            or ""
        ) or None
        ratio = _to_float(row.get("Кратность") or row.get("Коэффициент") or 1.0, 1.0)
        precision = _to_int(row.get("Точность"), None)

        if not name:
            name = code or ref_key

        existing = db.query(Unit).filter(Unit.unit_ref1c == ref_key).first()
        if existing:
            need_update = False
            if existing.unit_code != code:
                existing.unit_code = code; need_update = True
            if existing.unit_name != name:
                existing.unit_name = name; need_update = True
            if (existing.unit_full_name or "") != (full_name or None):
                existing.unit_full_name = full_name or None; need_update = True
            if (existing.short_name or "") != (short_name or None):
                existing.short_name = short_name or None; need_update = True
            if (existing.iso_code or "") != (iso_code or None):
                existing.iso_code = iso_code or None; need_update = True
            if (existing.base_unit_ref1c or "") != (base_unit_ref1c or None):
                existing.base_unit_ref1c = base_unit_ref1c or None; need_update = True
            if (existing.ratio or 1.0) != ratio:
                existing.ratio = ratio; need_update = True
            if (existing.precision or None) != precision:
                existing.precision = precision; need_update = True
            if need_update:
                updated += 1
        else:
            u = Unit(
                unit_ref1c=ref_key,
                unit_code=code,
                unit_name=name or "",
                unit_full_name=full_name or None,
                short_name=short_name or None,
                iso_code=iso_code or None,
                base_unit_ref1c=base_unit_ref1c or None,
                ratio=ratio,
                precision=precision,
            )
            db.add(u)
            created += 1
        found_keys.add(ref_key)

    # Пробуем подтянуть недостающие GUID из разных каталогов
    CHUNK = 20
    for cat in catalogs_to_probe:
        if not missing:
            break
        # Пробегаем по кусочкам
        missing_list = list(missing)
        for i in range(0, len(missing_list), CHUNK):
            chunk = missing_list[i:i + CHUNK]
            ors = " or ".join([f"Ref_Key eq guid'{g}'" for g in chunk])
            try:
                resp = client._make_request(cat, {
                    "$select": "Ref_Key,Code,Description,НаименованиеПолное,ПолноеНаименование,Сокращение,КраткоеНаименование,ISOCode,МеждународноеСокращение,БазоваяЕдиница_Key,БазоваяЕдиницаИзмерения_Key,BaseUnit_Key,Кратность,Коэффициент,Точность",
                    "$filter": f"({ors})"
                })
                rows = []
                if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
                    rows = resp["value"]
                elif resp:
                    rows = [resp]
                for r in rows:
                    upsert_from_row(r)
            except Exception as e:
                # Логируем, но продолжаем
                print(f"Backfill units: error probing {cat} for chunk {i}-{i+len(chunk)}: {e}")

        # После обработки каталога уменьшим missing
        missing -= found_keys

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise

    result["found"] = len(found_keys)
    result["created"] = created
    result["updated"] = updated
    result["missing_after"] = len(missing)
    return result