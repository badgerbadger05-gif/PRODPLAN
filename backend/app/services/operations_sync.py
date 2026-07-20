from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional, Set

from sqlalchemy.orm import Session

from ..schemas import ODataSyncRequest
from ..models import Operation
from ..services.odata_client import OData1CClient


def _s(val: Any) -> str:
    return str(val or "").strip()


# NOTE: kept local (not app.utils.numeric.to_float): this variant strips RU
# locale separators (comma decimal + nbsp/space thousands) before parsing, so
# it accepts "1 234,56" -> 1234.56 where the shared to_float would fall back.
def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        if isinstance(val, str):
            val = val.replace("\xa0", "").replace(" ", "").replace(",", ".")
        return float(val)
    except (TypeError, ValueError):
        return default


def _price_type_name(row: Dict[str, Any]) -> str:
    for key in ("ВидЦен", "ВидЦены", "ТипЦен", "ТипЦены", "price_type", "PriceType"):
        raw = row.get(key)
        if isinstance(raw, dict):
            name = _s(raw.get("Description") or raw.get("Наименование") or raw.get("Name"))
            if name:
                return name
        name = _s(raw)
        if name:
            return name
    return _s(row.get("ВидЦен_Description") or row.get("ВидЦены_Description"))


def _extract_operation_price(record: Dict[str, Any]) -> float:
    for key in ("Расценка", "Цена", "УчетнаяЦена", "УчетнаяЦена_Key", "operation_price"):
        price = _to_float(record.get(key), 0.0)
        if price > 0:
            return price

    candidates: list[tuple[bool, float]] = []
    for table_key in ("Цены", "Prices", "prices"):
        prices = record.get(table_key)
        if not isinstance(prices, list):
            continue
        for row in prices:
            if not isinstance(row, dict):
                continue
            price = 0.0
            for key in ("Цена", "Расценка", "Значение", "Value", "Price"):
                price = _to_float(row.get(key), 0.0)
                if price > 0:
                    break
            if price <= 0:
                continue
            price_type = _price_type_name(row).lower()
            is_accounting = "учет" in price_type or "учёт" in price_type
            candidates.append((is_accounting, price))

    if not candidates:
        return 0.0
    candidates.sort(key=lambda item: 0 if item[0] else 1)
    return candidates[0][1]


@dataclass
class OperationsSyncStats:
    """Статистика синхронизации наименований операций через строки спецификаций"""
    operations_total: int = 0            # ожидаемое общее число строк (по get_count), может быть больше числа уникальных операций
    operations_seen_unique: int = 0      # фактически встречено уникальных GUID операций
    operations_created: int = 0
    operations_updated: int = 0
    operations_unchanged: int = 0
    operation_prices_updated: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""               # ожидаем "Catalog_Спецификации_Операции"


def sync_operations_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация наименований операций из 1С через набор строк спецификаций "Catalog_Спецификации_Операции".
    Алгоритм:
      1) Идем постранично по сущности (req.entity_name, по умолчанию ожидаем "Catalog_Спецификации_Операции").
      2) Для каждой строки берём уникальный GUID операции: Операция_Key.
      3) Однократно по каждому GUID операции запрашиваем навигацию по полю "Операция@navigationLinkUrl"
         и забираем ее Description (и/или Code), НормаВремени и учетную цену.
      4) Upsert по Operation.operation_ref1c = Операция_Key.
      5) Прогресс: ключ "operations" через progress_manager.
    Примечания:
      - На некоторых конфигурациях $expand/$select может быть ограничен, поэтому используем навигацию из @navigationLinkUrl.
      - Для устойчивости стараемся не указывать узкие $select у основной сущности.
    """

    client = OData1CClient(req.base_url, req.username, req.password, req.token)

    stats = OperationsSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    # Прогресс
    try:
        from ..services.progress_manager import progress  # type: ignore
    except Exception:
        progress = None  # type: ignore

    entity_name = _s(req.entity_name or "Catalog_Спецификации_Операции")

    # Пытаемся получить общее количество строк
    total_count = 0
    try:
        total_count = client.get_count(entity_name, None)
    except Exception:
        total_count = 0

    stats.operations_total = int(total_count or 0)

    if progress:
        progress.start("operations", total=total_count or 0, message="Загрузка операций (через строки спецификаций)")

    # Подгрузим существующие операции для ускорения upsert
    existing_by_ref: Dict[str, Operation] = {
        o.operation_ref1c: o for o in db.query(Operation).all() if o.operation_ref1c
    }

    # Уникальные ключи операций, которые уже обработаны в текущей синхронизации
    processed_ops: Set[str] = set()

    created = 0
    updated = 0
    unchanged = 0
    prices_updated = 0
    seen_unique = 0

    processed_rows = 0

    try:
        # Идем страницами по строкам спецификаций; без узкого $select, чтобы не упасть на кастомных схемах
        for page in client.iter_pages(
            entity_name,
            filter_query=None,
            select_fields=None,  # тянем все поля, чтобы гарантировать наличие @navigationLinkUrl
            top=1000,
            max_pages=10000,
            order_by="Ref_Key",
        ):
            for row in page:
                processed_rows += 1

                # Обновляем прогресс по числу обработанных строк (ориентировочно)
                if progress and (processed_rows % 50 == 0):
                    msg = f"Обработано строк: {processed_rows}" + (f"/{total_count}" if total_count else "")
                    progress.update("operations", processed=processed_rows, message=msg)

                try:
                    op_key = _s(row.get("Операция_Key"))
                    if not op_key:
                        continue

                    if op_key in processed_ops:
                        continue
                    processed_ops.add(op_key)
                    seen_unique += 1

                    # Попробуем получить наименование по навигации
                    nav_url = row.get("Операция@navigationLinkUrl")
                    op_name: Optional[str] = None
                    op_time_norm = _to_float(row.get("НормаВремени"), 0.0)
                    op_price = 0.0
                    if nav_url:
                        try:
                            # Попытаемся аккуратно ограничить поля, если сервер позволит
                            resp = client._make_request(nav_url, {"$select": "Ref_Key,Code,Description,НормаВремени,Цена,Расценка,Цены"})
                        except Exception:
                            # Если сервер ругается на $select — повторим без параметров
                            try:
                                resp = client._make_request(nav_url, None)
                            except Exception:
                                resp = None

                        if isinstance(resp, dict):
                            name = _s(resp.get("Description"))
                            code = _s(resp.get("Code"))
                            op_name = name or code or None
                            op_time_norm = _to_float(resp.get("НормаВремени"), op_time_norm)
                            op_price = _extract_operation_price(resp)

                    # Фоллбэк: если нет навигации или она не вернула данных — оставим None
                    if not op_name:
                        # Не ставим пустое имя — просто пропускаем обновление имени
                        op_name = None

                    existing = existing_by_ref.get(op_key)
                    if existing:
                        # Обновим только если появилось новое читаемое имя
                        changed = False
                        if op_name and _s(existing.operation_name) != op_name:
                            existing.operation_name = op_name
                            changed = True
                        if op_time_norm > 0 and float(existing.time_norm or 0) != op_time_norm:
                            existing.time_norm = op_time_norm
                            changed = True
                        if op_price > 0 and float(existing.operation_price or 0) != op_price:
                            existing.operation_price = op_price
                            prices_updated += 1
                            changed = True
                        if changed:
                            updated += 1
                        else:
                            unchanged += 1
                    else:
                        # Создадим новую операцию даже если имя неизвестно (опционально)
                        # Это позволит связать позже при повторном прогоне, когда появится имя.
                        new_op = Operation(
                            operation_ref1c=op_key,
                            operation_name=op_name,
                            time_norm=op_time_norm,
                            operation_price=op_price,
                        )
                        db.add(new_op)
                        existing_by_ref[op_key] = new_op
                        created += 1

                    # Периодический промежуточный flush, чтобы не держать большой хвост
                    if (seen_unique % 500) == 0:
                        db.flush()

                except Exception as row_ex:
                    # Логируем и продолжаем
                    print(f"Ошибка обработки строки спецификации (операции): {row_ex}")
                    continue

        stats.operations_seen_unique = seen_unique

        # Финальный апдейт прогресса
        if progress:
            # Если был известен total, используем его, иначе просто зафиксируем прогресс по обработанным строкам
            processed_for_progress = stats.operations_total or processed_rows
            progress.update(
                "operations",
                processed=processed_for_progress,
                message=f"Готово: {processed_for_progress}/{stats.operations_total or processed_for_progress}",
            )
            progress.finish("operations", error=None, message="Синхронизация операций завершена")

        stats.operations_created = created
        stats.operations_updated = updated
        stats.operations_unchanged = unchanged
        stats.operation_prices_updated = prices_updated

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
                progress.finish("operations", error=str(e), message="Синхронизация операций завершилась ошибкой")
            except Exception:
                pass
        raise Exception(f"Ошибка синхронизации операций: {e}")
