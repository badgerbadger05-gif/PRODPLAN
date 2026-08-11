from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Dict, Optional, List, Iterable, DefaultDict
from datetime import datetime

from collections import defaultdict

from sqlalchemy.orm import Session

from ..models import (
    Item,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionStage,
    Specification,
    WorkshopWarehouseBinding,
)
from ..schemas import ODataSyncRequest
from .item_ledger.production_output_cache import (
    update_accepted_product_output_cache,
)


PRODUCTION_ORDER_SYNC_FROM = datetime(2026, 5, 1)
PRODUCTION_ORDER_SYNC_FROM_1C = "2026-05-01T00:00:00"


@dataclass
class ProductionOrderSyncStats:
    """Статистика синхронизации заказов на производство"""
    orders_total: int = 0
    orders_created: int = 0
    orders_updated: int = 0
    orders_unchanged: int = 0
    products_created: int = 0
    products_updated: int = 0
    products_failed: int = 0
    errors: List[str] = None  # type: ignore[assignment]
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def _chunked(seq: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _require_not_truncated(client, entity_name: str, *, context: str = "read") -> None:
    if getattr(client, "last_result_truncated", False):
        raise RuntimeError(
            f"OData {entity_name} {context} is truncated; refusing to sync"
        )


def _parse_1c_datetime(val) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _parse_1c_bool(val, default: bool = False) -> bool:
    """Корректная обработка boolean значений из 1С OData."""
    if val is None:
        return default
    # Иногда 1С/прокси возвращают scalar в обёртке-словаре
    # (например {"value": false} или {"Value": "false"}).
    if isinstance(val, dict):
        for k in ("value", "Value", "val", "boolean", "Boolean"):
            if k in val:
                return _parse_1c_bool(val.get(k), default)
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        # Для числовых значений принимаем только 0/1 как валидные bool.
        if val == 1:
            return True
        if val == 0:
            return False
        return default
    if isinstance(val, str):
        v = val.strip().lower()
        # EN
        if v in ("true", "1", "yes", "y", "on"):
            return True
        if v in ("false", "0", "no", "n", "off"):
            return False
        # RU (часто встречается при кастомных прокладках/логах)
        if v in ("истина", "да"):
            return True
        if v in ("ложь", "нет"):
            return False
        # fallback
        return default
    return default


def _norm_guid(val) -> str:
    """Нормализация GUID для сравнения (lowercase, без фигурных скобок, кавычек и обёрток)."""
    s = str(val or "").strip().lower()
    if not s:
        return ""
    # {xxxxxxxx-xxxx-...}
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    # guid'xxxxxxxx-xxxx-...'
    if s.startswith("guid'") and s.endswith("'"):
        s = s[len("guid'") : -1].strip()
    # 'xxxxxxxx-xxxx-...' (1С возвращает GUID в одинарных кавычках)
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1].strip()
    return s


def _nonzero_guid(val) -> Optional[str]:
    normalized = _norm_guid(val)
    if not normalized or normalized == "00000000-0000-0000-0000-000000000000":
        return None
    return normalized


def _resolve_local_ref_id(
    db: Session,
    id_column,
    ref_column,
    raw_key,
    cache: Dict[str, Optional[int]],
) -> Optional[int]:
    """
    Ref_Key из 1С -> локальный id справочника.

    Возвращает None, если ссылка пустая/нулевая или соответствующей записи в
    локальной базе нет. Вызывающий код обязан трактовать None как «неизвестно»,
    а не как «очистить значение».
    """
    normalized = _nonzero_guid(raw_key)
    if not normalized:
        return None
    if normalized in cache:
        return cache[normalized]
    row = (
        db.query(id_column)
        .filter(ref_column.isnot(None))
        .filter(ref_column.in_([normalized, str(raw_key or "").strip()]))
        .first()
    )
    resolved = int(row[0]) if row else None
    cache[normalized] = resolved
    return resolved


def _resolve_product_destination(
    db: Session,
    product_row: Dict,
    order_row: Dict,
    existing_product: Optional[ProductionProduct] = None,
) -> tuple[Optional[str], str]:
    """Resolve exact production inbound destination and its diagnostic source."""
    line_ref = _nonzero_guid(product_row.get("СтруктурнаяЕдиница_Key"))
    if line_ref:
        return line_ref, "line"
    header_ref = _nonzero_guid(
        order_row.get("СтруктурнаяЕдиницаПродукции_Key")
    )
    if header_ref:
        return header_ref, "header"
    if existing_product is not None and existing_product.product_id is not None:
        binding_ref = (
            db.query(WorkshopWarehouseBinding.production_warehouse_ref1c)
            .join(
                ProductionOrderLineState,
                ProductionOrderLineState.workshop_id
                == WorkshopWarehouseBinding.workshop_id,
            )
            .filter(
                ProductionOrderLineState.product_id == existing_product.product_id
            )
            .scalar()
        )
        binding_ref = _nonzero_guid(binding_ref)
        if binding_ref:
            return binding_ref, "binding"
    return None, "unresolved"


def sync_production_orders_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация заказов на производство из 1С через OData.

    Алгоритм:
    1. Загружаем все записи из Document_ЗаказНаПроизводство
    2. Для каждого заказа создаем или обновляем ProductionOrder
    3. Обрабатываем вложенные структуры:
       - Продукция (ProductionProduct)
       - Компоненты (ProductionComponent)
       - Операции (ProductionOperation)
    4. Обновляем статистику синхронизации
    """
    from ..services.odata_client import OData1CClient

    stats = ProductionOrderSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )
    # lazy-init for mutable default
    if stats.errors is None:
        stats.errors = []

    try:
        # Создаем клиент OData
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # --- 1) Заголовки заказов ---
        orders_select_default = [
            "Ref_Key",
            "Number",
            "Date",
            "Posted",
            "DeletionMark",
            "СостояниеЗаказа_Key",
            "СтруктурнаяЕдиницаПродукции_Key",
        ]
        
        # Фильтр заказов для расчёта:
        # - DeletionMark == false
        # - Posted == true
        # - Date >= 2026-05-01
        # Завершённые заказы намеренно синхронизируем: они должны попадать в
        # общую базу и участвовать в расчётах, а UI скрывает их отдельным правилом.
        # Дублируем фильтрацию в коде, т.к. 1С/прокси иногда частично игнорируют условия.
        # Серверный фильтр:
        # - Posted eq true (1С корректно фильтрует опубликованные)
        # - Date ge 2026-05-01, чтобы не тянуть старые завершённые хвосты из 1С.
        # DeletionMark фильтруем в коде, т.к. 1С игнорирует этот фильтр.
        default_filter = (
            f"Date ge datetime'{PRODUCTION_ORDER_SYNC_FROM_1C}' and "
            "Posted eq true"
        )

        # Даже если клиент прислал кастомный select_fields, принудительно добавляем
        # поля, нужные для корректной фильтрации активных заказов.
        effective_select_fields = list(req.select_fields or orders_select_default)
        for required_field in (
            "Posted",
            "DeletionMark",
            "СостояниеЗаказа_Key",
            "СтруктурнаяЕдиницаПродукции_Key",
        ):
            if required_field not in effective_select_fields:
                effective_select_fields.append(required_field)

        # Если пользователь передал доп. фильтр — учитываем его, но не даём вытащить удалённые.
        # (Защита от "перевёрнутой" выгрузки.)
        effective_filter = default_filter
        if getattr(req, "filter_query", None):
            effective_filter = f"({req.filter_query}) and ({default_filter})"
        
        order_data = client.get_all(
            req.entity_name,
            filter_query=effective_filter,
            select_fields=effective_select_fields,
            top=1000,
            max_pages=1000,
            order_by="Ref_Key",
        )
        _require_not_truncated(client, req.entity_name, context="header read")

        # Дублируем фильтр на уровне приложения: 1С / прокси иногда игнорируют часть условий.
        removed_by_dm = 0
        removed_by_posted = 0
        removed_by_missing_filter_fields = 0
        filtered_orders = []
        for rec in order_data:
            if "СостояниеЗаказа_Key" not in rec:
                raise RuntimeError(
                    "Order synchronization payload is missing required field "
                    "'СостояниеЗаказа_Key'"
                )

            # Проверяем наличие обязательных полей
            if rec.get("СостояниеЗаказа_Key") is None:
                removed_by_missing_filter_fields += 1
                continue

            # Фильтр DeletionMark: только не удалённые
            # (1С может не возвращать это поле, считаем False если отсутствует)
            dm = _parse_1c_bool(rec.get("DeletionMark"), False)
            if dm:
                removed_by_dm += 1
                continue
            
            # Фильтр Posted: только опубликованные заказы
            posted = _parse_1c_bool(rec.get("Posted"), False)
            if not posted:
                removed_by_posted += 1
                continue
            
            filtered_orders.append(rec)

        print(
            f"[DEBUG] Загружено: {len(order_data)}, "
            f"отфильтровано missing filter fields: {removed_by_missing_filter_fields}, "
            f"отфильтровано DeletionMark=true: {removed_by_dm}, "
            f"отфильтровано Posted=false: {removed_by_posted}, "
            f"итого к обработке: {len(filtered_orders)}"
        )
        order_data = filtered_orders

        if not order_data:
            stats.dry_run = True
            return asdict(stats)

        stats.orders_total = len(order_data)

        # Получаем существующие записи для сопоставления
        existing_orders = {
            order.order_ref1c: order
            for order in db.query(ProductionOrder).all()
            if order.order_ref1c
        }
        existing_items = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        products_created = 0
        products_updated = 0
        spec_id_cache: Dict[str, Optional[int]] = {}
        stage_id_cache: Dict[str, Optional[int]] = {}
        # --- 2) Строки продукции (отдельный EntitySet) ---
        # ВАЖНО: строки нужны нормализованные по LineNumber, т.к. item_id может повторяться в разных строках.
        products_entity = "Document_ЗаказНаПроизводство_Продукция"
        # Важно: 1С OData может не содержать некоторых полей (например, "Этап_Key"),
        # и тогда запрос с $select падает с 400.
        # Поэтому берём минимально-надёжный набор полей; общая политика
        # чтения OData зафиксирована в `.docs/odata.md`.
        products_select = [
            "Ref_Key",
            "LineNumber",
            "Номенклатура_Key",
            "Количество",
            "СтруктурнаяЕдиница_Key",
        ]
        # Спецификация/этап есть не в каждой конфигурации: пробуем расширенный
        # $select один раз и, если 1С его не принимает, навсегда откатываемся
        # на минимальный набор полей (см. фолбэк в цикле ниже).
        products_select_extended = products_select + [
            "Спецификация_Key",
            "Этап_Key",
        ]
        use_extended_products_select = True
        order_keys: List[str] = []
        for r in order_data:
            rk = str((r.get("Ref_Key") or "")).strip()
            if rk:
                order_keys.append(rk)

        products_by_order: DefaultDict[str, List[Dict]] = defaultdict(list)
        
        # Загружаем ВСЕ строки продукции ОДНИМ запросом (пакетная загрузка)
        # Фильтр: Ref_Key IN (список всех GUID заказов)
        if order_keys:
            # 1С OData поддерживает фильтр с множеством OR, но быстро упирается
            # во внутренний лимит вложенности выражений. Держим пачку небольшой:
            # 20 Ref_Key дают запас и по URL, и по глубине OR-дерева.
            BATCH_SIZE = 20
            for i in range(0, len(order_keys), BATCH_SIZE):
                batch_keys = order_keys[i:i + BATCH_SIZE]
                or_filter = " or ".join([f"Ref_Key eq guid'{k}'" for k in batch_keys])
                try:
                    try:
                        rows = client.get_all(
                            products_entity,
                            filter_query=f"({or_filter})",
                            select_fields=(
                                products_select_extended
                                if use_extended_products_select
                                else products_select
                            ),
                            top=1000,
                            max_pages=1000,
                            order_by="LineNumber",
                        )
                        _require_not_truncated(
                            client,
                            products_entity,
                            context=f"line read for {len(batch_keys)} orders",
                        )
                    except Exception:
                        if not use_extended_products_select:
                            raise
                        # Конфигурация без Спецификация_Key/Этап_Key: 1С отвечает
                        # 400 на такой $select. Дальше идём без этих полей.
                        use_extended_products_select = False
                        rows = client.get_all(
                            products_entity,
                            filter_query=f"({or_filter})",
                            select_fields=products_select,
                            top=1000,
                            max_pages=1000,
                            order_by="LineNumber",
                        )
                        _require_not_truncated(
                            client,
                            products_entity,
                            context=f"fallback line read for {len(batch_keys)} orders",
                        )
                    for pr in rows:
                        rk = str((pr.get("Ref_Key") or "")).strip()
                        if rk:
                            products_by_order[rk].append(pr)
                except Exception as e:
                    # Если пакет не загрузился, помечаем все заказы из пакета как failed
                    if isinstance(e, RuntimeError):
                        raise
                    for ok in batch_keys:
                        stats.products_failed += 1
                        stats.errors.append(f"Products load failed for order_ref1c={ok}: {e}")
                    # продолжаем синхронизацию заголовков, чтобы пользователь видел хотя бы шапки
                    continue

        # Обрабатываем каждый заказ
        for record in order_data:
            ref_key = ''
            try:
                ref_key = record.get('Ref_Key', '').strip()
                if not ref_key:
                    continue

                # Извлекаем данные заказа
                number = str(record.get("Number", "") or "").strip()
                order_date = _parse_1c_datetime(record.get("Date"))
                is_posted = _parse_1c_bool(record.get("Posted"), False)
                deletion_mark = _parse_1c_bool(record.get("DeletionMark"), False)
                order_state_key = _norm_guid(record.get("СостояниеЗаказа_Key", "")) or None

                if not number:
                    continue

                # Строки продукции берём из отдельного EntitySet
                products_data = products_by_order.get(ref_key, [])

                # Проверяем, существует ли уже такой заказ
                existing_order = existing_orders.get(ref_key)
                current_order = existing_order

                if existing_order:
                    # Проверяем, нужно ли обновлять
                    needs_update = (
                        existing_order.order_number != number or
                        existing_order.order_date != order_date or
                        existing_order.is_posted != is_posted or
                        getattr(existing_order, "deletion_mark", False) != deletion_mark or
                        getattr(existing_order, "order_state_key", None) != order_state_key
                    )

                    if needs_update:
                        existing_order.order_number = number
                        existing_order.order_date = order_date
                        existing_order.is_posted = is_posted
                        existing_order.deletion_mark = deletion_mark
                        existing_order.order_state_key = order_state_key
                        updated_count += 1
                    else:
                        unchanged_count += 1
                else:
                    # Создаем новый заказ
                    current_order = ProductionOrder(
                        order_number=number,
                        order_date=order_date,
                        order_ref1c=ref_key,
                        is_posted=is_posted,
                        deletion_mark=deletion_mark,
                        order_state_key=order_state_key,
                    )
                    db.add(current_order)
                    # Нужно получить order_id для строк
                    db.flush()
                    created_count += 1

                # Проверяем, что заказ создан или найден
                if not current_order:
                    continue

                # Обрабатываем продукцию заказа
                for prod_data in products_data:
                    try:
                        item_key = str(prod_data.get("Номенклатура_Key", "") or "").strip()
                        # Optional fields: some 1C configs expose them, but we don't require them for MVP
                        characteristic_key = str(prod_data.get("Характеристика_Key", "") or "").strip() or None
                        spec_key = str(prod_data.get("Спецификация_Key", "") or "").strip()
                        stage_key = str(prod_data.get("Этап_Key", "") or "").strip()
                        try:
                            line_number = int(prod_data.get("LineNumber")) if prod_data.get("LineNumber") is not None else None
                        except Exception:
                            line_number = None
                        try:
                            quantity = float(prod_data.get("Количество", 0.0) or 0.0)
                        except Exception:
                            quantity = 0.0

                        if not item_key:
                            continue

                        # Находим связанные объекты.
                        # spec_id/stage_id разрешаем по Ref_Key из 1С; None здесь
                        # означает «1С не дал ссылку либо она неизвестна локально»,
                        # и в этом случае существующая привязка сохраняется.
                        item = existing_items.get(item_key)
                        spec_id = _resolve_local_ref_id(
                            db,
                            Specification.spec_id,
                            Specification.spec_ref1c,
                            spec_key,
                            spec_id_cache,
                        )
                        stage_id = _resolve_local_ref_id(
                            db,
                            ProductionStage.stage_id,
                            ProductionStage.stage_ref1c,
                            stage_key,
                            stage_id_cache,
                        )

                        if not item:
                            continue

                        # Создаем или обновляем продукцию
                        existing_product = None
                        if line_number is not None:
                            existing_product = db.query(ProductionProduct).filter_by(
                                order_id=current_order.order_id,
                                line_number=line_number,
                            ).first()
                        else:
                            # fallback для старых/нестандартных ответов (неидеально, но лучше чем потерять данные)
                            existing_product = db.query(ProductionProduct).filter_by(
                                order_id=current_order.order_id,
                                item_id=item.item_id,
                            ).first()

                        destination_ref, _destination_source = _resolve_product_destination(
                            db, prod_data, record, existing_product
                        )

                        sync_product_fields = getattr(current_order, "source", "1c") != "mrp"
                        if existing_product:
                            if sync_product_fields:
                                # Нечего подставить — оставляем как есть, иначе каждая
                                # синхронизация обнуляла бы привязку спецификации/этапа.
                                target_spec_id = (
                                    spec_id if spec_id is not None else existing_product.spec_id
                                )
                                target_stage_id = (
                                    stage_id if stage_id is not None else existing_product.stage_id
                                )
                                if (existing_product.quantity != quantity or
                                    existing_product.spec_id != target_spec_id or
                                    existing_product.stage_id != target_stage_id or
                                    getattr(existing_product, "line_number", None) != line_number or
                                    getattr(existing_product, "characteristic_ref1c", None) != characteristic_key or
                                    existing_product.destination_warehouse_ref1c != destination_ref):
                                    existing_product.quantity = quantity
                                    existing_product.spec_id = target_spec_id
                                    existing_product.stage_id = target_stage_id
                                    existing_product.line_number = line_number
                                    existing_product.characteristic_ref1c = characteristic_key
                                    existing_product.destination_warehouse_ref1c = destination_ref
                                    products_updated += 1
                        elif sync_product_fields:
                            # Создаём новую строку продукции
                            # remaining_qty = quantity т.к. produced_qty = 0 для новой строки
                            new_product = ProductionProduct(
                                order_id=current_order.order_id,
                                item_id=item.item_id,
                                line_number=line_number,
                                characteristic_ref1c=characteristic_key,
                                destination_warehouse_ref1c=destination_ref,
                                quantity=quantity,
                                produced_qty=0.0,
                                remaining_qty=quantity,
                                spec_id=spec_id,
                                stage_id=stage_id,
                            )
                            db.add(new_product)
                            products_created += 1

                    except Exception as e:
                        print(f"Ошибка обработки продукции заказа {ref_key}: {e}")
                        continue

            except Exception as e:
                # Логируем ошибку, но продолжаем обработку
                print(f"Ошибка обработки заказа на производство {ref_key}: {e}")
                continue

        # Сохраняем изменения
        stats.orders_created = created_count
        stats.orders_updated = updated_count
        stats.orders_unchanged = unchanged_count
        stats.products_created = products_created
        stats.products_updated = products_updated

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

            # Новые/изменённые строки заказа сдвигают плановое количество, а
            # значит и кэш остатка выпуска. Пересчитываем его из принятого
            # Item Ledger — второго канала факта нет.
            try:
                fact_stats = sync_production_facts(db, req)
                print(f"[FACT CACHE] Status: {fact_stats.get('status')}, "
                      f"Facts: {fact_stats.get('facts', 0)}, "
                      f"Updated: {fact_stats.get('products_updated', 0)}")
            except Exception as e:
                print(f"[FACT CACHE WARNING] {e}")
                # Не прерываем основную синхронизацию из-за ошибки факта

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации заказов на производство: {e}")

    return asdict(stats)


FACT_CACHE_CONSUMER = "production_fact_cache"


def sync_production_facts(db: Session, req: Optional[ODataSyncRequest] = None) -> Dict[str, Any]:
    """
    Пересчитать кэш факта выпуска из ПРИНЯТОГО поколения Item Ledger.

    CANON: «Факт выпуска — считанный назад результат проведения
    `СборкаЗапасов` в принятом Item Ledger». Документы 1С здесь не читаются:
    параллельного движка факта нет. Функция — единственный писатель
    `ProductionProduct.produced_qty`; сами поля остаются кэшем чтения для
    журнала, гарда команды и возврата остатков компонентов.

    Fail-closed (`planning-truth-contract` §Fail closed): без принятого
    поколения кэш НЕ переписывается и НЕ обнуляется, а сводка возвращает
    `status="unavailable"` с причиной.

    `remaining_qty` — только совместимый кэш той же принятой проекции:
    `max(0, quantity - produced_qty)`. Терминальное состояние является
    отдельным операционным решением и не имеет права подменять физический
    остаток нулём.
    """
    from .planning_truth import (
        CAPABILITY_PHYSICAL_LEDGER,
        PlanningTruthUnavailable,
        require_accepted_truth,
    )
    from .item_ledger.physical_visibility import PhysicalVisibilityError
    from .item_ledger.production_fact_projection import derive_production_output
    dry_run = bool(getattr(req, "dry_run", False))

    try:
        truth = require_accepted_truth(
            db,
            FACT_CACHE_CONSUMER,
            (CAPABILITY_PHYSICAL_LEDGER,),
        )
    except PlanningTruthUnavailable as exc:
        readiness = exc.readiness
        return {
            "status": "unavailable",
            "source": "item_ledger",
            "truth_status": str(readiness.truth_status),
            "ledger_generation": readiness.ledger_generation,
            "cutoff": readiness.cutoff.isoformat() if readiness.cutoff else None,
            "reason": readiness.reason or str(exc),
            # CANON: неизвестный факт не показывается нулём.
            "facts": None,
            "products_updated": 0,
            "products_unchanged": None,
            "dry_run": dry_run,
        }

    try:
        projection = derive_production_output(
            db,
            ledger_generation_id=int(truth.generation_id),
        )
    except PhysicalVisibilityError as exc:
        return {
            "status": "unavailable",
            "source": "item_ledger",
            "truth_status": str(truth.truth_status),
            "ledger_generation": truth.generation_id,
            "cutoff": truth.cutoff.isoformat() if truth.cutoff else None,
            "reason": str(exc),
            # CANON: неизвестный факт не показывается нулём.
            "facts": None,
            "products_updated": 0,
            "products_unchanged": None,
            "dry_run": dry_run,
        }

    products = db.query(ProductionProduct).all()
    products_updated = 0
    products_unchanged = 0
    zero = Decimal("0")
    for product in products:
        product_id = int(product.product_id)
        derived = projection.produced_by_product.get(product_id, zero)
        if not update_accepted_product_output_cache(
            product,
            produced_qty=derived,
        ):
            products_unchanged += 1
            continue
        products_updated += 1

    if dry_run:
        db.rollback()
    else:
        db.commit()

    print(
        f"[FACT CACHE] generation={projection.ledger_generation_id} "
        f"facts={projection.facts} matched={projection.matched_facts} "
        f"ambiguous={projection.ambiguous_facts} "
        f"unmatched={projection.unmatched_facts} updated={products_updated}"
    )

    return {
        "status": "ok",
        "source": "item_ledger",
        "truth_status": str(truth.truth_status),
        "ledger_generation": projection.ledger_generation_id,
        "cutoff": projection.cutoff.isoformat() if projection.cutoff else None,
        "reason": None,
        "facts": projection.facts,
        "fact_qty": float(projection.fact_qty),
        "matched_facts": projection.matched_facts,
        "matched_qty": float(projection.matched_qty),
        "exact_link_facts": projection.exact_link_facts,
        "order_scope_facts": projection.order_scope_facts,
        "ambiguous_facts": projection.ambiguous_facts,
        "unmatched_facts": projection.unmatched_facts,
        "surplus_qty": float(projection.surplus_qty),
        "products_updated": products_updated,
        "products_unchanged": products_unchanged,
        "dry_run": dry_run,
    }
