from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import ItemCategory
from ..schemas import ODataSyncRequest


@dataclass
class CategorySyncStats:
    """Статистика синхронизации категорий номенклатуры"""
    categories_total: int = 0
    categories_created: int = 0
    categories_updated: int = 0
    categories_unchanged: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def sync_categories_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация категорий номенклатуры из 1С через OData.

    Алгоритм:
    1. Загружаем все записи из Catalog_КатегорииНоменклатуры
    2. Строим иерархию категорий по Parent_Key
    3. Для каждой категории создаем или обновляем ItemCategory
    4. Обновляем статистику синхронизации
    """
    from ..services.odata_client import OData1CClient

    stats = CategorySyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    try:
        # Создаем клиент OData
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # Получаем все записи категорий
        category_data = client.get_all(
            req.entity_name,
            filter_query=req.filter_query,
            select_fields=req.select_fields
        )

        if not category_data:
            stats.dry_run = True
            return asdict(stats)

        stats.categories_total = len(category_data)

        # Получаем существующие записи для сопоставления
        existing_categories = {cat.category_ref1c: cat for cat in db.query(ItemCategory).all() if cat.category_ref1c}

        # Строим карту родительских категорий
        parent_map = {}
        for record in category_data:
            ref_key = record.get('Ref_Key', '').strip()
            parent_key = record.get('Parent_Key', '').strip()
            if ref_key and parent_key:
                parent_map[ref_key] = parent_key

        created_count = 0
        updated_count = 0
        unchanged_count = 0

        # Обрабатываем каждую запись категории
        for record in category_data:
            ref_key = ''
            try:
                ref_key = record.get('Ref_Key', '').strip()
                if not ref_key:
                    continue

                # Извлекаем данные категории
                code = record.get('Code', '').strip()
                name = record.get('Description', '').strip()
                parent_key = record.get('Parent_Key', '').strip()
                is_folder = record.get('IsFolder', False)
                predefined = record.get('Predefined', False)
                predefined_name = record.get('PredefinedDataName', '').strip()
                data_version = record.get('DataVersion', '').strip()
                deletion_mark = record.get('DeletionMark', False)

                if not name:
                    continue

                # Определяем parent_id
                parent_id = None
                if parent_key and parent_key in existing_categories:
                    parent_id = existing_categories[parent_key].category_id

                # Проверяем, существует ли уже такая категория
                existing_category = existing_categories.get(ref_key)

                if existing_category:
                    # Проверяем, нужно ли обновлять
                    needs_update = (
                        existing_category.category_code != code or
                        existing_category.category_name != name or
                        existing_category.parent_id != parent_id or
                        existing_category.is_folder != is_folder or
                        existing_category.predefined != predefined or
                        existing_category.predefined_name != predefined_name or
                        existing_category.data_version != data_version or
                        existing_category.deletion_mark != deletion_mark
                    )

                    if needs_update:
                        existing_category.category_code = code
                        existing_category.category_name = name
                        existing_category.parent_id = parent_id
                        existing_category.is_folder = is_folder
                        existing_category.predefined = predefined
                        existing_category.predefined_name = predefined_name
                        existing_category.data_version = data_version
                        existing_category.deletion_mark = deletion_mark
                        updated_count += 1
                    else:
                        unchanged_count += 1
                else:
                    # Создаем новую категорию
                    new_category = ItemCategory(
                        category_code=code,
                        category_name=name,
                        category_ref1c=ref_key,
                        parent_id=parent_id,
                        is_folder=is_folder,
                        predefined=predefined,
                        predefined_name=predefined_name,
                        data_version=data_version,
                        deletion_mark=deletion_mark
                    )
                    db.add(new_category)
                    created_count += 1

            except Exception as e:
                # Логируем ошибку, но продолжаем обработку
                print(f"Ошибка обработки записи категории {ref_key}: {e}")
                continue

        # Сохраняем изменения
        stats.categories_created = created_count
        stats.categories_updated = updated_count
        stats.categories_unchanged = unchanged_count

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации категорий: {e}")

    return asdict(stats)