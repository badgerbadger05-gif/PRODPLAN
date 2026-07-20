from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List, Iterable, DefaultDict, Tuple
from datetime import datetime

from collections import defaultdict

from sqlalchemy.orm import Session

from ..models import ProductionOrder, ProductionProduct, Item
from ..schemas import ODataSyncRequest


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
        for required_field in ("Posted", "DeletionMark", "СостояниеЗаказа_Key"):
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

        # Диагностика: посмотреть "сырые" значения полей, которые участвуют в фильтрации.
        # Это нужно, чтобы выявить расхождение между серверным OData-фильтром и пост-фильтром приложения.
        for i, rec in enumerate(order_data[:5]):
            raw_dm = rec.get("DeletionMark")
            raw_posted = rec.get("Posted")
            raw_state = rec.get("СостояниеЗаказа_Key")
            print(
                f"[DEBUG RAW {i}] Ref_Key={rec.get('Ref_Key')}, Number={rec.get('Number')}, "
                f"DeletionMark={raw_dm!r} (type={type(raw_dm).__name__}, parsed={_parse_1c_bool(raw_dm, False)}), "
                f"Posted={raw_posted!r} (type={type(raw_posted).__name__}, parsed={_parse_1c_bool(raw_posted, False)}), "
                f"СостояниеЗаказа_Key={raw_state!r}"
            )

        # Дублируем фильтр на уровне приложения: 1С / прокси иногда игнорируют часть условий.
        removed_by_dm = 0
        removed_by_posted = 0
        removed_by_missing_filter_fields = 0
        filtered_orders = []
        for rec in order_data:
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
        
        # Логирование для отладки: первые 5 заказов
        for i, rec in enumerate(order_data[:5]):
            dm = rec.get('DeletionMark')
            print(f"[DEBUG Order {i}] Ref_Key={rec.get('Ref_Key')}, Number={rec.get('Number')}, "
                  f"DeletionMark={dm} (type={type(dm).__name__}), СостояниеЗаказа_Key={rec.get('СостояниеЗаказа_Key')}")

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
        # --- 2) Строки продукции (отдельный EntitySet) ---
        # ВАЖНО: строки нужны нормализованные по LineNumber, т.к. item_id может повторяться в разных строках.
        products_entity = "Document_ЗаказНаПроизводство_Продукция"
        # Важно: 1С OData может не содержать некоторых полей (например, "Этап_Key"),
        # и тогда запрос с $select падает с 400.
        # Поэтому для MVP берём минимально-надёжный набор полей (как в `.docs/production_orders_odata_queries.md`).
        products_select = [
            "Ref_Key",
            "LineNumber",
            "Номенклатура_Key",
            "Количество",
        ]
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
                    rows = client.get_all(
                        products_entity,
                        filter_query=f"({or_filter})",
                        select_fields=products_select,
                        top=1000,
                        max_pages=1000,
                        order_by="LineNumber",
                    )
                    for pr in rows:
                        rk = str((pr.get("Ref_Key") or "")).strip()
                        if rk:
                            products_by_order[rk].append(pr)
                except Exception as e:
                    # Если пакет не загрузился, помечаем все заказы из пакета как failed
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

                        # Находим связанные объекты
                        item = existing_items.get(item_key)
                        spec = None
                        stage = None

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

                        if existing_product:
                            if (existing_product.quantity != quantity or
                                existing_product.spec_id != (spec.spec_id if spec else None) or
                                existing_product.stage_id != (stage.stage_id if stage else None) or
                                getattr(existing_product, "line_number", None) != line_number or
                                getattr(existing_product, "characteristic_ref1c", None) != characteristic_key):
                                existing_product.quantity = quantity
                                existing_product.spec_id = spec.spec_id if spec else None
                                existing_product.stage_id = stage.stage_id if stage else None
                                existing_product.line_number = line_number
                                existing_product.characteristic_ref1c = characteristic_key
                                products_updated += 1
                        else:
                            # Создаём новую строку продукции
                            # remaining_qty = quantity т.к. produced_qty = 0 для новой строки
                            new_product = ProductionProduct(
                                order_id=current_order.order_id,
                                item_id=item.item_id,
                                line_number=line_number,
                                characteristic_ref1c=characteristic_key,
                                quantity=quantity,
                                produced_qty=0.0,
                                remaining_qty=quantity,
                                spec_id=spec.spec_id if spec else None,
                                stage_id=stage.stage_id if stage else None
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
            # Правило: "отсутствует в загрузке = закрыт" действует только в
            # синхронизируемом горизонте. Старые локальные заказы до 2026-05-01
            # не трогаем, иначе date-фильтр превращает их в ложные удаления.
            loaded_order_refs = set(order_keys)
            orders_to_close = []
            for order in db.query(ProductionOrder).filter(
                ProductionOrder.deletion_mark == False,
                ProductionOrder.order_date >= PRODUCTION_ORDER_SYNC_FROM,
            ).all():
                if order.order_ref1c and order.order_ref1c not in loaded_order_refs:
                    orders_to_close.append(order)
            
            if orders_to_close:
                print(f"[SYNC] Closing {len(orders_to_close)} orders not found in 1C load")
                for order in orders_to_close:
                    order.deletion_mark = True
            
            db.commit()

            # После успешной синхронизации заказов и продукции,
            # загружаем факт выпуска из сборок (Сборка запасов)
            # и обновляем produced_qty / remaining_qty
            try:
                fact_stats = sync_production_fact_from_odata(db, req)
                print(f"[FACT SYNC] Assemblies: {fact_stats.get('assemblies_loaded', 0)}, "
                      f"Products: {fact_stats.get('assembly_products_loaded', 0)}, "
                      f"Updated: {fact_stats.get('products_updated', 0)}")
            except Exception as e:
                print(f"[FACT SYNC WARNING] {e}")
                # Не прерываем основную синхронизацию из-за ошибки факта

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации заказов на производство: {e}")

    return asdict(stats)


def sync_production_fact_from_odata(db: Session, req: ODataSyncRequest) -> Dict[str, any]:
    """
    Синхронизация факта выпуска из 1С через OData.
    
    Источник: Document_СборкаЗапасов и Document_СборкаЗапасов_Продукция.
    
    Алгоритм:
    1. Загружаем все активные заказы из БД для маппинга
    2. Для каждого заказа загружаем СборкаЗапасов с фильтром по ЗаказНаПроизводство_Key
    3. Загружаем продукцию сборок
    4. Агрегируем produced_qty по (order_ref1c, item_ref1c, characteristic_ref1c)
    5. Обновляем ProductionProduct.produced_qty и remaining_qty
    """
    from ..services.odata_client import OData1CClient
    from ..models import Item
    
    stats = {
        "assemblies_loaded": 0,
        "assembly_products_loaded": 0,
        "products_updated": 0,
        "orders_skipped_missing_ref": 0,
        "errors": [],
        "dry_run": bool(req.dry_run),
    }
    
    try:
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # --- 1) Загружаем все активные заказы из БД для маппинга ---
        active_orders = db.query(ProductionOrder).filter(
            ProductionOrder.deletion_mark == False
        ).all()
        orders_by_ref1c = {}
        for order in active_orders:
            order_ref1c = str(order.order_ref1c or "").strip()
            if not order_ref1c:
                stats["orders_skipped_missing_ref"] += 1
                continue
            orders_by_ref1c[order_ref1c] = order

        if stats["orders_skipped_missing_ref"]:
            print(
                "[FACT SYNC] Skipping "
                f"{stats['orders_skipped_missing_ref']} active orders without order_ref1c"
            )

        if not orders_by_ref1c:
            stats["dry_run"] = True
            return stats

        # --- 2) Загружаем СборкаЗапасов для каждого заказа ---
        # Агрегация produced_qty по (order_ref1c, item_ref1c, characteristic_ref1c)
        produced_agg: DefaultDict[Tuple[str, str, str], float] = defaultdict(float)
        
        order_keys = list(orders_by_ref1c.keys())
        print(f"[FACT SYNC] Processing {len(order_keys)} orders")

        # Загружаем сборки для каждого заказа отдельно (чтобы избежать проблем с длинными URL)
        for i, order_ref1c in enumerate(order_keys):
            if i % 20 == 0:
                print(f"[FACT SYNC] Progress: {i}/{len(order_keys)}")
            
            try:
                # Загружаем сборки для заказа
                assemblies = client.get_all(
                    "Document_СборкаЗапасов",
                    filter_query=f"ЗаказНаПроизводство_Key eq guid'{order_ref1c}' and Posted eq true",
                    select_fields=["Ref_Key", "ЗаказНаПроизводство_Key", "DeletionMark"],
                    top=1000,
                    max_pages=10,
                )
                
                # Фильтруем DeletionMark в коде
                assembly_keys = []
                for asm in assemblies:
                    dm = _parse_1c_bool(asm.get("DeletionMark"), False)
                    if not dm:
                        ak = str(asm.get("Ref_Key") or "").strip()
                        if ak:
                            assembly_keys.append(ak)
                            stats["assemblies_loaded"] += 1
                
                if not assembly_keys:
                    continue
                
                # Загружаем продукцию сборок
                # Используем expand чтобы получить продукцию одним запросом
                for j, assembly_key in enumerate(assembly_keys):
                    if j % 50 == 0:
                        print(f"[FACT SYNC] Order {order_ref1c}: assembly products progress {j}/{len(assembly_keys)}")
                    try:
                        assembly_products = client.get_all(
                            "Document_СборкаЗапасов_Продукция",
                            filter_query=f"Ref_Key eq guid'{assembly_key}'",
                            select_fields=["Номенклатура_Key", "Характеристика_Key", "Количество"],
                            top=1000,
                            max_pages=5,
                        )
                        
                        for prod in assembly_products:
                            item_key = str(prod.get("Номенклатура_Key", "") or "").strip()
                            char_key = str(prod.get("Характеристика_Key", "") or "").strip() or ""
                            try:
                                qty = float(prod.get("Количество", 0.0) or 0.0)
                            except Exception:
                                qty = 0.0
                            
                            if not item_key:
                                continue
                            
                            # Агрегируем по (order, item, characteristic)
                            agg_key = (order_ref1c, item_key, char_key)
                            produced_agg[agg_key] += qty
                            stats["assembly_products_loaded"] += 1
                            
                    except Exception as e:
                        print(f"[FACT SYNC] Error loading assembly products: {e}")
                        continue
                        
            except Exception as e:
                print(f"[FACT SYNC] Error for order {order_ref1c}: {e}")
                stats["errors"].append(f"Order {order_ref1c}: {e}")
                continue

        if not produced_agg:
            print("[FACT SYNC] No production facts found")
            stats["dry_run"] = True
            return stats

        # --- 3) Обновляем ProductionProduct ---
        products_updated = 0
        products_not_found = 0

        for (order_ref1c, item_ref1c, char_ref1c), produced_qty in produced_agg.items():
            if order_ref1c not in orders_by_ref1c:
                continue

            order = orders_by_ref1c[order_ref1c]

            # Находим ProductionProduct по (order_id, item_id, characteristic_ref1c)
            product = None

            # Находим item по item_ref1c
            item = db.query(Item).filter_by(item_ref1c=item_ref1c).first()
            if not item:
                products_not_found += 1
                continue

            # Поиск с characteristic (если есть)
            if char_ref1c:
                product = db.query(ProductionProduct).filter_by(
                    order_id=order.order_id,
                    item_id=item.item_id,
                    characteristic_ref1c=char_ref1c,
                ).first()

            # Поиск без characteristic
            if not product:
                product = db.query(ProductionProduct).filter_by(
                    order_id=order.order_id,
                    item_id=item.item_id,
                ).first()

            if product:
                from decimal import Decimal
                produced_qty_dec = Decimal(str(produced_qty))
                product.produced_qty = float(produced_qty_dec)
                product.remaining_qty = float(max(product.quantity - produced_qty_dec, Decimal('0')))
                products_updated += 1
            else:
                products_not_found += 1

        print(f"[FACT SYNC] Assemblies: {stats['assemblies_loaded']}, "
              f"Products: {stats['assembly_products_loaded']}, "
              f"Updated: {products_updated}, Not found: {products_not_found}")

        stats["products_updated"] = products_updated

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации факта выпуска: {e}")

    return stats
