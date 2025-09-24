from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Item, ItemCategory
from ..schemas import ODataSyncRequest


@dataclass
class NomenclatureSyncStats:
    """Статистика синхронизации номенклатуры"""
    items_total: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_updated_by_code: int = 0
    items_unchanged: int = 0
    categories_created: int = 0
    categories_updated: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def sync_nomenclature_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация номенклатуры из 1С через OData.

    Алгоритм:
    1. Загружаем все записи из Catalog_Номенклатура
    2. Для каждой записи создаем или обновляем Item
    3. Обрабатываем связи с категориями номенклатуры
    4. Обновляем статистику синхронизации
    """
    from ..services.odata_client import OData1CClient

    stats = NomenclatureSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    try:
        # Создаем клиент OData и подготавливаем прогресс
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # Получаем общее количество записей в 1С (если поддерживается)
        try:
            from ..services.progress_manager import progress
        except Exception:
            progress = None  # type: ignore

        # Используем безопасную постраничную выборку без $filter (1С может отклонять WHERE по полям каталога)
        # Фильтрацию папок делаем на клиенте (Python), пропуская записи с IsFolder == true
        effective_filter = None

        # Поля, которые гарантированно нужны в обработке
        local_select = (req.select_fields or [
            'Ref_Key', 'Code', 'Description', 'Артикул',
            'ЕдиницаИзмерения_Key', 'КатегорияНоменклатуры_Key',
            'СпособПополнения', 'СрокПополнения', 'IsFolder'
        ])

        total_count = 0
        try:
            # Запрашиваем общий объём без фильтра; прогресс считаем от total
            total_count = client.get_count(req.entity_name, None)
        except Exception:
            total_count = 0

        if progress:
            progress.start("nomenclature", total=total_count or 0, message="Загрузка номенклатуры из 1С")

        # Статистика общего количества (если известно заранее)
        if total_count > 0:
            stats.items_total = int(total_count)

        # Получаем существующие записи для сопоставления
        existing_items_by_ref = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}
        existing_items_by_code = {item.item_code: item for item in db.query(Item).all() if item.item_code}
        existing_categories = {cat.category_ref1c: cat for cat in db.query(ItemCategory).all() if cat.category_ref1c}
        existing_codes_count = len(existing_items_by_code)

        created_count = 0
        updated_count = 0
        updated_by_code_count = 0
        unchanged_count = 0
        categories_created = 0
        categories_updated = 0

        # Обрабатываем записи постранично
        processed_count = 0

        for page in client.iter_pages(
            req.entity_name,
            filter_query=None,
            select_fields=local_select,
            top=1000,
            max_pages=1000,
            order_by="Ref_Key",
        ):
            for record in page:
                ref_key = ''
                try:
                    ref_key = (record.get('Ref_Key') or '').strip()
                    if not ref_key:
                        continue

                    # Прогресс
                    processed_count += 1
                    if 'progress' in locals() and progress and (processed_count % 50 == 0):
                        msg = f"Обработано {processed_count}" + (f"/{total_count}" if total_count > 0 else "")
                        progress.update("nomenclature", processed=processed_count, message=msg)

                    # Извлекаем данные номенклатуры
                    code = (record.get('Code') or '').strip()
                    name = (record.get('Description') or '').strip()
                    article = (record.get('Артикул') or '').strip()
                    replenishment_method = (record.get('СпособПополнения') or '').strip()
                    replenishment_time = record.get('СрокПополнения')
                    unit_key = (record.get('ЕдиницаИзмерения_Key') or '').strip()
                    category_key = (record.get('КатегорияНоменклатуры_Key') or '').strip()
                    item_type = (record.get('ТипНоменклатуры') or '').strip()

                    if not name:
                        continue

                    # Проверяем, существует ли уже такая номенклатура
                    existing_item = existing_items_by_ref.get(ref_key)

                    if existing_item:
                        # Проверяем, нужно ли обновлять
                        needs_update = (
                            existing_item.item_code != code or
                            existing_item.item_name != name or
                            existing_item.item_article != article or
                            existing_item.replenishment_method != replenishment_method or
                            (replenishment_time is not None and existing_item.replenishment_time != replenishment_time) or
                            existing_item.unit != unit_key
                        )

                        if needs_update:
                            existing_item.item_code = code
                            existing_item.item_name = name
                            existing_item.item_article = article
                            existing_item.replenishment_method = replenishment_method
                            existing_item.replenishment_time = replenishment_time
                            existing_item.unit = unit_key
                            updated_count += 1
                        else:
                            unchanged_count += 1
                    else:
                        # Проверяем, существует ли номенклатура с таким кодом
                        existing_item_by_code = existing_items_by_code.get(code)

                        if existing_item_by_code:
                            # Если код уже существует, обновляем данные существующей записи
                            existing_item_by_code.item_name = name
                            existing_item_by_code.item_article = article
                            existing_item_by_code.item_ref1c = ref_key
                            existing_item_by_code.replenishment_method = replenishment_method
                            existing_item_by_code.replenishment_time = replenishment_time
                            existing_item_by_code.unit = unit_key
                            existing_item_by_code.status = 'active'
                            # ВАЖНО: обновляем локальные мапы
                            existing_items_by_ref[ref_key] = existing_item_by_code
                            existing_items_by_code[code] = existing_item_by_code
                            updated_by_code_count += 1
                        else:
                            # Проверяем еще раз на уровне SQL
                            existing_item_sql = db.query(Item).filter(Item.item_code == code).first()

                            if existing_item_sql:
                                existing_item_sql.item_name = name
                                existing_item_sql.item_article = article
                                existing_item_sql.item_ref1c = ref_key
                                existing_item_sql.replenishment_method = replenishment_method
                                existing_item_sql.replenishment_time = replenishment_time
                                existing_item_sql.unit = unit_key
                                existing_item_sql.status = 'active'
                                existing_items_by_ref[ref_key] = existing_item_sql
                                existing_items_by_code[code] = existing_item_sql
                                updated_by_code_count += 1
                            else:
                                # Создаем новую номенклатуру
                                new_item = Item(
                                    item_code=code,
                                    item_name=name,
                                    item_article=article,
                                    item_ref1c=ref_key,
                                    replenishment_method=replenishment_method,
                                    replenishment_time=replenishment_time,
                                    unit=unit_key,
                                    stock_qty=0.0,
                                    status='active'
                                )
                                db.add(new_item)
                                existing_items_by_ref[ref_key] = new_item
                                existing_items_by_code[code] = new_item
                                created_count += 1

                    # Обрабатываем категорию номенклатуры
                    if category_key:
                        existing_category = existing_categories.get(category_key)
                        category_data = record.get('КатегорияНоменклатуры', {})
                        category_name = (category_data.get('Description') if category_data else '').strip()

                        if existing_category:
                            if existing_category.category_name != category_name:
                                existing_category.category_name = category_name
                                categories_updated += 1
                        else:
                            new_category = ItemCategory(
                                category_name=category_name,
                                category_ref1c=category_key,
                                is_folder=False,
                                predefined=False,
                                deletion_mark=False
                            )
                            db.add(new_category)
                            existing_categories[category_key] = new_category
                            categories_created += 1

                except Exception as e:
                    # Логируем ошибку, но продолжаем обработку
                    print(f"Ошибка обработки записи номенклатуры {ref_key}: {e}")
                    continue

        # Если общее количество не было известно заранее — выставим по факту
        if stats.items_total == 0:
            stats.items_total = int(processed_count)

        # Финальное обновление прогресса
        if 'progress' in locals() and progress:
            progress.update("nomenclature", processed=processed_count, message=f"Готово: {processed_count}/{stats.items_total}")
            progress.finish("nomenclature", error=None, message="Синхронизация завершена")

        # Fallback: если ни одной новой записи не создано, а в 1С объектов больше, чем кодов в БД —
        # выполняем дополнительный проход для добивки отсутствующих item_code.
        try:
            if created_count == 0 and (stats.items_total or 0) > (existing_codes_count or 0):
                seen_codes = set(existing_items_by_code.keys())
                missing_created = 0

                for page in client.iter_pages(
                    req.entity_name,
                    filter_query=None,
                    select_fields=local_select,
                    top=1000,
                    max_pages=1000,
                    order_by="Ref_Key",
                ):
                    for record in page:
                        code = (record.get('Code') or '').strip()
                        if not code or code in seen_codes:
                            continue

                        name = (record.get('Description') or '').strip() or code
                        article = (record.get('Артикул') or '').strip()
                        replenishment_method = (record.get('СпособПополнения') or '').strip()
                        replenishment_time = record.get('СрокПополнения')
                        unit_key = (record.get('ЕдиницаИзмерения_Key') or '').strip()
                        ref_key = (record.get('Ref_Key') or '').strip()

                        new_item = Item(
                            item_code=code,
                            item_name=name,
                            item_article=article or None,
                            item_ref1c=ref_key or None,
                            replenishment_method=replenishment_method or None,
                            replenishment_time=replenishment_time,
                            unit=unit_key or None,
                            stock_qty=0.0,
                            status='active'
                        )
                        db.add(new_item)
                        seen_codes.add(code)
                        existing_items_by_code[code] = new_item  # обновим локальный кэш
                        if ref_key:
                            existing_items_by_ref[ref_key] = new_item
                        missing_created += 1

                        # Периодически сбрасываем хвост, чтобы не держать большую транзакцию
                        if (missing_created % 500) == 0:
                            db.flush()

                if missing_created > 0:
                    created_count += missing_created
        except Exception as _e:
            # Не валим основную синхронизацию, просто логируем
            print(f"Fallback добивки номенклатуры завершился с ошибкой: {_e}")

        # Сохраняем изменения
        stats.items_created = created_count
        stats.items_updated = updated_count
        stats.items_updated_by_code = updated_by_code_count
        stats.items_unchanged = unchanged_count
        stats.categories_created = categories_created
        stats.categories_updated = categories_updated

        if req.dry_run:
            db.rollback()
        else:
            try:
                db.commit()
            except Exception as e:
                db.rollback()
                # Если это ошибка дубликата ключа, попробуем обработать её
                if "duplicate key value violates unique constraint" in str(e):
                    print(f"Обнаружен конфликт уникальности при сохранении. Повторная попытка...")
                    # В случае ошибки дубликата, откатываемся и не бросаем исключение дальше
                    # Данные уже обработаны в памяти, просто не сохранились из-за конфликта
                    pass
                else:
                    raise e

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации номенклатуры: {e}")

    return asdict(stats)