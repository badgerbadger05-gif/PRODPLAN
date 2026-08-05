from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import (
    Specification,
    SpecComponent,
    Operation,
    SpecOperation,
    Item,
    ProductionStage,
    ProductionKind,
    ProductionManufactureOperation,
)
from ..schemas import ODataSyncRequest

_ZERO_GUID = "00000000-0000-0000-0000-000000000000"


def _norm_component_spec_ref(value) -> Optional[str]:
    """
    Нормализует Спецификация_Key строки состава 1С к каноничному виду.

    Пустая строка и нулевой GUID -> None (компонент идёт по основной спецификации).
    Иначе — guid в нижнем регистре. Значимо только для строк типа Сборка/Узел.
    """
    raw = str(value or "").strip().lower()
    if not raw or raw == _ZERO_GUID:
        return None
    return raw


@dataclass
class SpecificationSyncStats:
    """Статистика синхронизации спецификаций"""
    specs_total: int = 0
    specs_created: int = 0
    specs_updated: int = 0
    specs_unchanged: int = 0
    components_created: int = 0
    components_updated: int = 0
    components_deleted: int = 0
    operations_created: int = 0
    operations_updated: int = 0
    spec_operations_created: int = 0
    spec_operations_updated: int = 0
    spec_operations_deleted: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def sync_specifications_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация спецификаций из 1С через OData.

    Алгоритм:
    1. Загружаем все записи из Catalog_Спецификации
    2. Для каждой спецификации создаем или обновляем Specification
    3. Обрабатываем вложенные структуры:
       - Состав спецификаций (SpecComponent)
       - Операции (Operation и SpecOperation)
    4. Обновляем статистику синхронизации
    """
    from ..services.odata_client import OData1CClient

    stats = SpecificationSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    try:
        # Создаем клиент OData
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # Получаем все записи спецификаций.
        # IMPORTANT:
        # - Для корректной синхронизации состава/операций и работы reconcile нам нужны поля 'Состав' и 'Операции'.
        # - Пользовательский $select может их случайно не включить, что приводит к «зависшим» строкам в БД.
        # Поэтому принудительно добавляем эти поля к select_fields.
        base_select = list(req.select_fields or [])
        required_fields = ["Ref_Key", "Code", "Description", "ВидПроизводства_Key", "Состав", "Операции"]
        for f in required_fields:
            if f not in base_select:
                base_select.append(f)
        effective_select = base_select or None

        try:
            spec_data = client.get_all(
                req.entity_name,
                filter_query=req.filter_query,
                select_fields=effective_select,
            )
        except Exception as e:
            # Fallback: на некоторых конфигурациях 1С узкий $select может падать.
            # Повторяем запрос без $select, чтобы всё же получить вложенные табличные части.
            print(f"[spec.sync] primary get_all failed, retry without $select: {e}")
            spec_data = client.get_all(
                req.entity_name,
                filter_query=req.filter_query,
                select_fields=None,
            )

        if not spec_data:
            stats.dry_run = True
            return asdict(stats)

        stats.specs_total = len(spec_data)

        # Получаем существующие записи для сопоставления
        existing_specs = {spec.spec_ref1c: spec for spec in db.query(Specification).all() if spec.spec_ref1c}
        existing_operations = {op.operation_ref1c: op for op in db.query(Operation).all() if op.operation_ref1c}
        existing_production_kinds = {pk.ref_1c: pk for pk in db.query(ProductionKind).all() if pk.ref_1c}

        # Получаем существующие номенклатуру и этапы для связей
        existing_items = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}
        existing_stages = {stage.stage_ref1c: stage for stage in db.query(ProductionStage).all() if stage.stage_ref1c}

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        components_created = 0
        components_updated = 0
        components_deleted = 0
        operations_created = 0
        operations_updated = 0
        spec_operations_created = 0
        spec_operations_updated = 0
        spec_operations_deleted = 0

        # Обрабатываем каждую спецификацию
        for record in spec_data:
            ref_key = ''
            try:
                ref_key = record.get('Ref_Key', '').strip()
                if not ref_key:
                    continue

                # Извлекаем данные спецификации
                code = record.get('Code', '').strip()
                name = record.get('Description', '').strip()

                if not name:
                    continue

                # Обрабатываем состав спецификаций
                # IMPORTANT: reconcile (delete removed rows) выполняем только если поле реально присутствует в ответе OData.
                # Если поле не выгружено (select_fields не содержит вложенную табличную часть), мы НЕ имеем права удалять строки.
                has_components_field = 'Состав' in record
                components_data_raw = record.get('Состав', None)
                components_data: list = []
                can_reconcile_components = False
                if isinstance(components_data_raw, list):
                    components_data = components_data_raw
                    can_reconcile_components = bool(has_components_field)
                else:
                    components_data = []
                    can_reconcile_components = False

                # Обрабатываем операции
                has_operations_field = 'Операции' in record
                operations_data_raw = record.get('Операции', None)
                operations_data: list = []
                can_reconcile_operations = False
                if isinstance(operations_data_raw, list):
                    operations_data = operations_data_raw
                    can_reconcile_operations = bool(has_operations_field)
                else:
                    operations_data = []
                    can_reconcile_operations = False

                # Проверяем, существует ли уже такая спецификация
                existing_spec = existing_specs.get(ref_key)
                current_spec = existing_spec

                # Извлекаем ВидПроизводства из данных
                production_kind_key = record.get('ВидПроизводства_Key', '').strip()
                production_kind = None
                if production_kind_key:
                    production_kind = existing_production_kinds.get(production_kind_key)

                if existing_spec:
                    # Проверяем, нужно ли обновлять
                    needs_update = (
                        existing_spec.spec_code != code or
                        existing_spec.spec_name != name or
                        existing_spec.production_kind_id != (production_kind.id if production_kind else None)
                    )

                    if needs_update:
                        existing_spec.spec_code = code
                        existing_spec.spec_name = name
                        existing_spec.production_kind_id = production_kind.id if production_kind else None
                        updated_count += 1
                    else:
                        unchanged_count += 1
                else:
                    # Создаем новую спецификацию
                    current_spec = Specification(
                        spec_code=code,
                        spec_name=name,
                        spec_ref1c=ref_key,
                        production_kind_id=production_kind.id if production_kind else None
                    )
                    db.add(current_spec)
                    # Важно: получить spec_id до создания связанных записей
                    db.flush()
                    created_count += 1
                    # Добавим в индекс, чтобы исключить повторные вставки при дублирующихся записях
                    existing_specs[ref_key] = current_spec

                # Проверяем, что спецификация создана или найдена
                if not current_spec:
                    continue

                # Обрабатываем компоненты спецификации.
                # Естественный ключ строки состава — тройка (spec_id, item_id, закреплённая спека),
                # потому что один и тот же компонент может присутствовать в одной спецификации
                # несколько раз с разными Спецификация_Key (тип Сборка/Узел).
                seen_component_keys: set[tuple[int, Optional[str]]] = set()
                for comp_data in components_data:
                    try:
                        comp_ref_key = comp_data.get('Ref_Key', '').strip()
                        if not comp_ref_key:
                            continue

                        item_key = comp_data.get('Номенклатура_Key', '').strip()
                        quantity = comp_data.get('Количество', 0.0)
                        stage_key = comp_data.get('Этап_Key', '').strip()
                        component_type = comp_data.get('ТипСтрокиСостава', 'Материал')
                        component_spec_ref1c = _norm_component_spec_ref(comp_data.get('Спецификация_Key'))

                        # Находим связанные объекты
                        item = existing_items.get(item_key)
                        stage = existing_stages.get(stage_key) if stage_key else None

                        if not item:
                            continue

                        seen_component_keys.add((int(item.item_id), component_spec_ref1c))

                        # Создаем или обновляем компонент по полному естественному ключу
                        existing_comp = db.query(SpecComponent).filter_by(
                            spec_id=current_spec.spec_id,
                            item_id=item.item_id,
                            component_spec_ref1c=component_spec_ref1c
                        ).first()

                        if existing_comp:
                            # Real 1C OData may omit component stage data. Keep a
                            # previously curated/backfilled stage instead of
                            # erasing it to NULL on every sync.
                            desired_stage_id = stage.stage_id if stage else existing_comp.stage_id
                            if (existing_comp.quantity != quantity or
                                existing_comp.stage_id != desired_stage_id or
                                existing_comp.component_type != component_type):
                                existing_comp.quantity = quantity
                                existing_comp.stage_id = desired_stage_id
                                existing_comp.component_type = component_type
                                components_updated += 1
                        else:
                            new_comp = SpecComponent(
                                spec_id=current_spec.spec_id,
                                item_id=item.item_id,
                                quantity=quantity,
                                stage_id=stage.stage_id if stage else None,
                                component_type=component_type,
                                component_spec_ref1c=component_spec_ref1c
                            )
                            db.add(new_comp)
                            components_created += 1

                    except Exception as e:
                        print(f"Ошибка обработки компонента спецификации {ref_key}: {e}")
                        continue

                # Reconcile: удалить строки состава, которых больше нет в 1С
                # (только если поле 'Состав' реально выгружено). Сверяем по полному
                # естественному ключу (item_id, закреплённая спека), а не только item_id,
                # иначе легальные дубли «один компонент с разными спеками» удалялись бы.
                if can_reconcile_components:
                    try:
                        # SAVEPOINT: ошибка удаления не должна абортировать всю
                        # транзакцию синка (иначе остальные спецификации падают
                        # каскадом PendingRollbackError).
                        with db.begin_nested():
                            deleted = 0
                            existing_rows = (
                                db.query(SpecComponent)
                                .filter(SpecComponent.spec_id == current_spec.spec_id)
                                .all()
                            )
                            for row in existing_rows:
                                if (int(row.item_id), row.component_spec_ref1c) not in seen_component_keys:
                                    db.delete(row)
                                    deleted += 1
                            if deleted:
                                components_deleted += int(deleted)
                    except Exception as e:
                        print(f"Ошибка reconcile компонентов спецификации {ref_key}: {e}")

                # Обрабатываем операции спецификации
                seen_operation_ids: set[int] = set()
                for op_data in operations_data:
                    try:
                        operation_key = op_data.get('Операция_Key', '').strip()
                        time_norm = op_data.get('НормаВремени', 0.0)
                        stage_key = op_data.get('Этап_Key', '').strip()

                        if not operation_key:
                            continue

                        # Находим или создаем операцию
                        operation = existing_operations.get(operation_key)
                        if not operation:
                            operation = Operation(
                                operation_ref1c=operation_key,
                                time_norm=time_norm
                            )
                            db.add(operation)
                            # Получить operation_id, чтобы корректно создавать SpecOperation
                            db.flush()
                            operations_created += 1
                            # Обновить локальный индекс, чтобы избежать повторных вставок с тем же ключом
                            existing_operations[operation_key] = operation
                        else:
                            if operation.time_norm != time_norm:
                                operation.time_norm = time_norm
                                operations_updated += 1
                            # Страхуемся, что в индексе присутствует объект операции
                            existing_operations[operation_key] = operation

                        if operation and operation.operation_id is not None:
                            seen_operation_ids.add(int(operation.operation_id))

                        # Находим этап
                        stage = existing_stages.get(stage_key) if stage_key else None

                        # Создаем или обновляем связь спецификация-операция
                        existing_spec_op = db.query(SpecOperation).filter_by(
                            spec_id=current_spec.spec_id,
                            operation_id=operation.operation_id
                        ).first()

                        if existing_spec_op:
                            # Same preservation rule as for components: an
                            # absent stage in OData must not wipe local stage
                            # knowledge used by capacity/MRP views.
                            desired_stage_id = stage.stage_id if stage else existing_spec_op.stage_id
                            updated = False
                            if existing_spec_op.stage_id != (desired_stage_id):
                                existing_spec_op.stage_id = desired_stage_id
                                updated = True
                            if existing_spec_op.time_norm != time_norm:
                                existing_spec_op.time_norm = time_norm
                                updated = True
                            if updated:
                                spec_operations_updated += 1
                        else:
                            new_spec_op = SpecOperation(
                                spec_id=current_spec.spec_id,
                                operation_id=operation.operation_id,
                                stage_id=stage.stage_id if stage else None,
                                time_norm=time_norm
                            )
                            db.add(new_spec_op)
                            spec_operations_created += 1

                    except Exception as e:
                        print(f"Ошибка обработки операции спецификации {ref_key}: {e}")
                        continue

                # Reconcile: удалить связи спецификация-операция, которые больше не присутствуют в 1С (только если поле 'Операции' выгружено).
                # Строки, на которые ссылаются операции изготовлений
                # (production_manufacture_operations.spec_operation_id), не удаляем:
                # изготовление обязано сохранять свою производственную основу,
                # а попытка удаления валит FK и абортирует транзакцию.
                if can_reconcile_operations:
                    try:
                        with db.begin_nested():
                            referenced_ops = (
                                db.query(ProductionManufactureOperation.spec_operation_id)
                                .filter(ProductionManufactureOperation.spec_operation_id.isnot(None))
                                .subquery()
                            )
                            q_del = db.query(SpecOperation).filter(
                                SpecOperation.spec_id == current_spec.spec_id,
                                ~SpecOperation.spec_operation_id.in_(referenced_ops.select()),
                            )
                            if seen_operation_ids:
                                q_del = q_del.filter(~SpecOperation.operation_id.in_(list(seen_operation_ids)))
                            deleted = q_del.delete(synchronize_session=False)
                            if deleted:
                                spec_operations_deleted += int(deleted)
                    except Exception as e:
                        print(f"Ошибка reconcile операций спецификации {ref_key}: {e}")

            except Exception as e:
                # Логируем ошибку, но продолжаем обработку
                print(f"Ошибка обработки спецификации {ref_key}: {e}")
                if not db.is_active:
                    # После ошибки flush сессия непригодна: все накопленные
                    # изменения уже потеряны, каждая следующая спецификация
                    # упадёт каскадом PendingRollbackError. Поднимаем исходную
                    # ошибку, чтобы job завершился с настоящей причиной.
                    raise
                continue

        # Сохраняем изменения
        stats.specs_created = created_count
        stats.specs_updated = updated_count
        stats.specs_unchanged = unchanged_count
        stats.components_created = components_created
        stats.components_updated = components_updated
        stats.components_deleted = components_deleted
        stats.operations_created = operations_created
        stats.operations_updated = operations_updated
        stats.spec_operations_created = spec_operations_created
        stats.spec_operations_updated = spec_operations_updated
        stats.spec_operations_deleted = spec_operations_deleted

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации спецификаций: {e}")

    return asdict(stats)
