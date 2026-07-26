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
    duplicates_deleted: int = 0
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

        # Получаем существующие записи для сопоставления.
        # Ключ должен быть (item_id, characteristic_id), т.к. spec_id может меняться.
        # Нормализуем «пустую характеристику» (None, пустая строка, GUID нулей) к одному представлению.
        def _norm_char(val: str | None) -> str:
            v = (str(val or '').strip() if val is not None else '').strip()
            if not v:
                return ''
            # В 1С часто встречается «нулевой GUID» как пустая характеристика
            if v == '00000000-0000-0000-0000-000000000000':
                return ''
            return v

        existing_by_key: dict[tuple[int, str], DefaultSpecification] = {}
        existing_all: list[DefaultSpecification] = db.query(DefaultSpecification).all()
        for rec in existing_all:
            key = (int(rec.item_id), _norm_char(getattr(rec, 'characteristic_id', None)))
            # Если в БД есть дубликаты, предпочтём самую свежую запись (updated_at/created_at), иначе большую id.
            prev = existing_by_key.get(key)
            if prev is None:
                existing_by_key[key] = rec
            else:
                prev_ts = getattr(prev, 'updated_at', None) or getattr(prev, 'created_at', None)
                rec_ts = getattr(rec, 'updated_at', None) or getattr(rec, 'created_at', None)
                if rec_ts and prev_ts and rec_ts > prev_ts:
                    existing_by_key[key] = rec
                elif rec_ts == prev_ts:
                    try:
                        if int(getattr(rec, 'id')) > int(getattr(prev, 'id')):
                            existing_by_key[key] = rec
                    except Exception:
                        pass

        # Получаем существующие номенклатуру и спецификации для связей
        existing_items = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}
        existing_specs = {spec.spec_ref1c: spec for spec in db.query(Specification).all() if spec.spec_ref1c}

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        duplicates_deleted = 0

        touched_keys: set[tuple[int, str]] = set()

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

                char_norm = _norm_char(characteristic_key)
                record_key = (int(item.item_id), char_norm)
                touched_keys.add(record_key)

                # Проверяем, существует ли уже такая запись
                existing_record = existing_by_key.get(record_key)

                if existing_record:
                    # Upsert: если spec_id изменился — обновляем
                    if int(existing_record.spec_id) != int(spec.spec_id):
                        existing_record.spec_id = int(spec.spec_id)
                        # Нормализуем characteristic_id в БД (чтобы не плодить дублей)
                        existing_record.characteristic_id = (char_norm or None)
                        updated_count += 1
                    else:
                        # Запись уже существует, считаем её неизменной
                        unchanged_count += 1
                else:
                    # Создаем новую запись
                    new_record = DefaultSpecification(
                        item_id=item.item_id,
                        characteristic_id=(char_norm or None),
                        spec_id=spec.spec_id
                    )
                    db.add(new_record)
                    created_count += 1
                    # Обновим in-memory индекс (чтобы в рамках одного прогона не создавать дублей)
                    existing_by_key[record_key] = new_record

            except Exception as e:
                # Логируем ошибку, но продолжаем обработку
                print(f"Ошибка обработки записи спецификации по умолчанию: {e}")
                continue

        # Cleanup: если в БД уже накопились дубли (item_id, characteristic_id) — удалим лишние,
        # оставив самую новую запись.
        if touched_keys:
            try:
                touched_item_ids = list({iid for (iid, _) in touched_keys})
                rows = (
                    db.query(DefaultSpecification)
                    .filter(DefaultSpecification.item_id.in_(touched_item_ids))
                    .all()
                )
                grouped: dict[tuple[int, str], list[DefaultSpecification]] = {}
                for r in rows:
                    k = (int(r.item_id), _norm_char(getattr(r, 'characteristic_id', None)))
                    if k in touched_keys:
                        grouped.setdefault(k, []).append(r)

                for k, lst in grouped.items():
                    if len(lst) <= 1:
                        continue
                    # keep: max(updated_at/created_at, id)
                    def sort_key(x: DefaultSpecification):
                        ts = getattr(x, 'updated_at', None) or getattr(x, 'created_at', None)
                        try:
                            xid = int(getattr(x, 'id'))
                        except Exception:
                            xid = 0
                        return (ts is not None, ts, xid)

                    keep = sorted(lst, key=sort_key, reverse=True)[0]
                    for r in lst:
                        if r is keep:
                            continue
                        try:
                            db.delete(r)
                            duplicates_deleted += 1
                        except Exception:
                            continue
            except Exception as e:
                print(f"Ошибка очистки дублей default_specifications: {e}")

        # Сохраняем изменения
        stats.records_created = created_count
        stats.records_updated = updated_count
        stats.records_unchanged = unchanged_count
        stats.duplicates_deleted = duplicates_deleted

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации спецификаций по умолчанию: {e}")

    return asdict(stats)
