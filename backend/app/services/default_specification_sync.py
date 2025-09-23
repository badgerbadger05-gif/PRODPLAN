from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import DefaultSpecification, Item, Specification
from ..schemas import ODataSyncRequest


@dataclass
class DefaultSpecificationSyncStats:
    """Статистика синхронизации спецификаций по умолчанию"""
    records_total: int = 0
    records_created: int = 0
    records_updated: int = 0
    records_unchanged: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def sync_default_specifications_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация спецификаций по умолчанию из 1С через OData.

    Алгоритм:
    1. Загружаем все записи из InformationRegister_СпецификацииПоУмолчанию
    2. Для каждой записи создаем или обновляем DefaultSpecification
    3. Обновляем статистику синхронизации
    """
    from ..services.odata_client import OData1CClient

    stats = DefaultSpecificationSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    try:
        # Создаем клиент OData
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # Получаем все записи спецификаций по умолчанию
        # Информационный регистр обычно не имеет Ref_Key, поэтому убираем $orderby=Ref_Key
        # и подставляем безопасный набор полей, если select_fields не задан
        safe_select = req.select_fields or [
            "Номенклатура_Key",
            "Характеристика_Key",
            "Спецификация_Key",
        ]
        spec_data = client.get_all(
            req.entity_name,
            filter_query=req.filter_query,
            select_fields=safe_select,
            order_by=None  # важно: не добавлять $orderby=Ref_Key для регистров
        )

        if not spec_data:
            stats.dry_run = True
            return asdict(stats)

        stats.records_total = len(spec_data)

        # Получаем существующие записи для сопоставления
        existing_records = {}
        for record in db.query(DefaultSpecification).all():
            key = (record.item_id, record.characteristic_id or '', record.spec_id)
            existing_records[key] = record

        # Получаем существующие номенклатуру и спецификации для связей
        existing_items = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}
        existing_specs = {spec.spec_ref1c: spec for spec in db.query(Specification).all() if spec.spec_ref1c}

        created_count = 0
        updated_count = 0
        unchanged_count = 0

        # Обрабатываем каждую запись
        for record in spec_data:
            try:
                # Извлекаем данные записи
                item_key = record.get('Номенклатура_Key', '').strip()
                characteristic_key = record.get('Характеристика_Key', '').strip()
                spec_key = record.get('Спецификация_Key', '').strip()

                if not item_key or not spec_key:
                    continue

                # Находим связанные объекты
                item = existing_items.get(item_key)
                spec = existing_specs.get(spec_key)

                if not item or not spec:
                    continue

                # Создаем ключ для поиска существующей записи
                record_key = (item.item_id, characteristic_key, spec.spec_id)

                # Проверяем, существует ли уже такая запись
                existing_record = existing_records.get(record_key)

                if existing_record:
                    # Запись уже существует, считаем её неизменной
                    unchanged_count += 1
                else:
                    # Создаем новую запись
                    new_record = DefaultSpecification(
                        item_id=item.item_id,
                        characteristic_id=characteristic_key if characteristic_key else None,
                        spec_id=spec.spec_id
                    )
                    db.add(new_record)
                    created_count += 1

            except Exception as e:
                # Логируем ошибку, но продолжаем обработку
                print(f"Ошибка обработки записи спецификации по умолчанию: {e}")
                continue

        # Сохраняем изменения
        stats.records_created = created_count
        stats.records_updated = updated_count
        stats.records_unchanged = unchanged_count

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации спецификаций по умолчанию: {e}")

    return asdict(stats)