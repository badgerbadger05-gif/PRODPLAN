from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Specification, SpecComponent, Operation, SpecOperation, Item, ProductionStage
from ..schemas import ODataSyncRequest


@dataclass
class SpecificationSyncStats:
    """Статистика синхронизации спецификаций"""
    specs_total: int = 0
    specs_created: int = 0
    specs_updated: int = 0
    specs_unchanged: int = 0
    components_created: int = 0
    components_updated: int = 0
    operations_created: int = 0
    operations_updated: int = 0
    spec_operations_created: int = 0
    spec_operations_updated: int = 0
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

        # Получаем все записи спецификаций
        spec_data = client.get_all(
            req.entity_name,
            filter_query=req.filter_query,
            select_fields=req.select_fields
        )

        if not spec_data:
            stats.dry_run = True
            return asdict(stats)

        stats.specs_total = len(spec_data)

        # Получаем существующие записи для сопоставления
        existing_specs = {spec.spec_ref1c: spec for spec in db.query(Specification).all() if spec.spec_ref1c}
        existing_operations = {op.operation_ref1c: op for op in db.query(Operation).all() if op.operation_ref1c}

        # Получаем существующие номенклатуру и этапы для связей
        existing_items = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}
        existing_stages = {stage.stage_ref1c: stage for stage in db.query(ProductionStage).all() if stage.stage_ref1c}

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        components_created = 0
        components_updated = 0
        operations_created = 0
        operations_updated = 0
        spec_operations_created = 0
        spec_operations_updated = 0

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
                components_data = record.get('Состав', [])
                if not isinstance(components_data, list):
                    components_data = []

                # Обрабатываем операции
                operations_data = record.get('Операции', [])
                if not isinstance(operations_data, list):
                    operations_data = []

                # Проверяем, существует ли уже такая спецификация
                existing_spec = existing_specs.get(ref_key)
                current_spec = existing_spec

                if existing_spec:
                    # Проверяем, нужно ли обновлять
                    needs_update = (
                        existing_spec.spec_code != code or
                        existing_spec.spec_name != name
                    )

                    if needs_update:
                        existing_spec.spec_code = code
                        existing_spec.spec_name = name
                        updated_count += 1
                    else:
                        unchanged_count += 1
                else:
                    # Создаем новую спецификацию
                    current_spec = Specification(
                        spec_code=code,
                        spec_name=name,
                        spec_ref1c=ref_key
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

                # Обрабатываем компоненты спецификации
                for comp_data in components_data:
                    try:
                        comp_ref_key = comp_data.get('Ref_Key', '').strip()
                        if not comp_ref_key:
                            continue

                        item_key = comp_data.get('Номенклатура_Key', '').strip()
                        quantity = comp_data.get('Количество', 0.0)
                        stage_key = comp_data.get('Этап_Key', '').strip()
                        component_type = comp_data.get('ТипСтрокиСостава', 'Материал')

                        # Находим связанные объекты
                        item = existing_items.get(item_key)
                        stage = existing_stages.get(stage_key) if stage_key else None

                        if not item:
                            continue

                        # Создаем или обновляем компонент
                        existing_comp = db.query(SpecComponent).filter_by(
                            spec_id=current_spec.spec_id,
                            item_id=item.item_id
                        ).first()

                        if existing_comp:
                            if (existing_comp.quantity != quantity or
                                existing_comp.stage_id != (stage.stage_id if stage else None) or
                                existing_comp.component_type != component_type):
                                existing_comp.quantity = quantity
                                existing_comp.stage_id = stage.stage_id if stage else None
                                existing_comp.component_type = component_type
                                components_updated += 1
                        else:
                            new_comp = SpecComponent(
                                spec_id=current_spec.spec_id,
                                item_id=item.item_id,
                                quantity=quantity,
                                stage_id=stage.stage_id if stage else None,
                                component_type=component_type
                            )
                            db.add(new_comp)
                            components_created += 1

                    except Exception as e:
                        print(f"Ошибка обработки компонента спецификации {ref_key}: {e}")
                        continue

                # Обрабатываем операции спецификации
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

                        # Находим этап
                        stage = existing_stages.get(stage_key) if stage_key else None

                        # Создаем или обновляем связь спецификация-операция
                        existing_spec_op = db.query(SpecOperation).filter_by(
                            spec_id=current_spec.spec_id,
                            operation_id=operation.operation_id
                        ).first()

                        if existing_spec_op:
                            if existing_spec_op.stage_id != (stage.stage_id if stage else None):
                                existing_spec_op.stage_id = stage.stage_id if stage else None
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

            except Exception as e:
                # Логируем ошибку, но продолжаем обработку
                print(f"Ошибка обработки спецификации {ref_key}: {e}")
                continue

        # Сохраняем изменения
        stats.specs_created = created_count
        stats.specs_updated = updated_count
        stats.specs_unchanged = unchanged_count
        stats.components_created = components_created
        stats.components_updated = components_updated
        stats.operations_created = operations_created
        stats.operations_updated = operations_updated
        stats.spec_operations_created = spec_operations_created
        stats.spec_operations_updated = spec_operations_updated

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации спецификаций: {e}")

    return asdict(stats)