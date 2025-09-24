from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import ProductionOrder, ProductionProduct, ProductionComponent, ProductionOperation, Item, Specification, Operation, ProductionStage
from ..schemas import ODataSyncRequest


@dataclass
class ProductionOrderSyncStats:
    """Статистика синхронизации заказов на производство"""
    orders_total: int = 0
    orders_created: int = 0
    orders_updated: int = 0
    orders_unchanged: int = 0
    products_created: int = 0
    products_updated: int = 0
    components_created: int = 0
    components_updated: int = 0
    operations_created: int = 0
    operations_updated: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


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

    try:
        # Создаем клиент OData
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # Получаем все записи заказов на производство
        order_data = client.get_all(
            req.entity_name,
            filter_query=req.filter_query,
            select_fields=req.select_fields
        )

        if not order_data:
            stats.dry_run = True
            return asdict(stats)

        stats.orders_total = len(order_data)

        # Получаем существующие записи для сопоставления
        existing_orders = {order.order_ref1c: order for order in db.query(ProductionOrder).all() if order.order_ref1c}
        existing_items = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}
        existing_specs = {spec.spec_ref1c: spec for spec in db.query(Specification).all() if spec.spec_ref1c}
        existing_operations = {op.operation_ref1c: op for op in db.query(Operation).all() if op.operation_ref1c}
        existing_stages = {stage.stage_ref1c: stage for stage in db.query(ProductionStage).all() if stage.stage_ref1c}

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        products_created = 0
        products_updated = 0
        components_created = 0
        components_updated = 0
        operations_created = 0
        operations_updated = 0

        # Обрабатываем каждый заказ
        for record in order_data:
            ref_key = ''
            try:
                ref_key = record.get('Ref_Key', '').strip()
                if not ref_key:
                    continue

                # Извлекаем данные заказа
                number = record.get('Number', '').strip()
                date_str = record.get('Date', '')
                is_posted = record.get('Posted', False)

                # Конвертируем дату
                order_date = None
                if date_str:
                    try:
                        if isinstance(date_str, str):
                            order_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        else:
                            order_date = date_str
                    except:
                        order_date = datetime.now()

                if not number:
                    continue

                # Обрабатываем продукцию
                products_data = record.get('Продукция', [])
                if not isinstance(products_data, list):
                    products_data = []

                # Обрабатываем компоненты
                components_data = record.get('Запасы', [])
                if not isinstance(components_data, list):
                    components_data = []

                # Обрабатываем операции
                operations_data = record.get('Операции', [])
                if not isinstance(operations_data, list):
                    operations_data = []

                # Проверяем, существует ли уже такой заказ
                existing_order = existing_orders.get(ref_key)
                current_order = existing_order

                if existing_order:
                    # Проверяем, нужно ли обновлять
                    needs_update = (
                        existing_order.order_number != number or
                        existing_order.order_date != order_date or
                        existing_order.is_posted != is_posted
                    )

                    if needs_update:
                        existing_order.order_number = number
                        existing_order.order_date = order_date
                        existing_order.is_posted = is_posted
                        updated_count += 1
                    else:
                        unchanged_count += 1
                else:
                    # Создаем новый заказ
                    current_order = ProductionOrder(
                        order_number=number,
                        order_date=order_date,
                        order_ref1c=ref_key,
                        is_posted=is_posted
                    )
                    db.add(current_order)
                    created_count += 1

                # Проверяем, что заказ создан или найден
                if not current_order:
                    continue

                # Обрабатываем продукцию заказа
                for prod_data in products_data:
                    try:
                        item_key = prod_data.get('Номенклатура_Key', '').strip()
                        quantity = prod_data.get('Количество', 0.0)
                        spec_key = prod_data.get('Спецификация_Key', '').strip()
                        stage_key = prod_data.get('Этап_Key', '').strip()

                        if not item_key:
                            continue

                        # Находим связанные объекты
                        item = existing_items.get(item_key)
                        spec = existing_specs.get(spec_key) if spec_key else None
                        stage = existing_stages.get(stage_key) if stage_key else None

                        if not item:
                            continue

                        # Создаем или обновляем продукцию
                        existing_product = db.query(ProductionProduct).filter_by(
                            order_id=current_order.order_id,
                            item_id=item.item_id
                        ).first()

                        if existing_product:
                            if (existing_product.quantity != quantity or
                                existing_product.spec_id != (spec.spec_id if spec else None) or
                                existing_product.stage_id != (stage.stage_id if stage else None)):
                                existing_product.quantity = quantity
                                existing_product.spec_id = spec.spec_id if spec else None
                                existing_product.stage_id = stage.stage_id if stage else None
                                products_updated += 1
                        else:
                            new_product = ProductionProduct(
                                order_id=current_order.order_id,
                                item_id=item.item_id,
                                quantity=quantity,
                                spec_id=spec.spec_id if spec else None,
                                stage_id=stage.stage_id if stage else None
                            )
                            db.add(new_product)
                            products_created += 1

                    except Exception as e:
                        print(f"Ошибка обработки продукции заказа {ref_key}: {e}")
                        continue

                # Обрабатываем компоненты заказа
                for comp_data in components_data:
                    try:
                        item_key = comp_data.get('Номенклатура_Key', '').strip()
                        quantity = comp_data.get('Количество', 0.0)
                        spec_key = comp_data.get('Спецификация_Key', '').strip()
                        stage_key = comp_data.get('Этап_Key', '').strip()

                        if not item_key:
                            continue

                        # Находим связанные объекты
                        item = existing_items.get(item_key)
                        spec = existing_specs.get(spec_key) if spec_key else None
                        stage = existing_stages.get(stage_key) if stage_key else None

                        if not item:
                            continue

                        # Создаем или обновляем компонент
                        existing_component = db.query(ProductionComponent).filter_by(
                            order_id=current_order.order_id,
                            item_id=item.item_id
                        ).first()

                        if existing_component:
                            if (existing_component.quantity != quantity or
                                existing_component.spec_id != (spec.spec_id if spec else None) or
                                existing_component.stage_id != (stage.stage_id if stage else None)):
                                existing_component.quantity = quantity
                                existing_component.spec_id = spec.spec_id if spec else None
                                existing_component.stage_id = stage.stage_id if stage else None
                                components_updated += 1
                        else:
                            new_component = ProductionComponent(
                                order_id=current_order.order_id,
                                item_id=item.item_id,
                                quantity=quantity,
                                spec_id=spec.spec_id if spec else None,
                                stage_id=stage.stage_id if stage else None
                            )
                            db.add(new_component)
                            components_created += 1

                    except Exception as e:
                        print(f"Ошибка обработки компонента заказа {ref_key}: {e}")
                        continue

                # Обрабатываем операции заказа
                for op_data in operations_data:
                    try:
                        operation_key = op_data.get('Операция_Key', '').strip()
                        planned_quantity = op_data.get('КоличествоПлан', 0.0)
                        time_norm = op_data.get('НормаВремени', 0.0)
                        standard_hours = op_data.get('Нормочасы', 0.0)
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
                            operations_created += 1
                        else:
                            if operation.time_norm != time_norm:
                                operation.time_norm = time_norm
                                operations_updated += 1

                        # Находим этап
                        stage = existing_stages.get(stage_key) if stage_key else None

                        # Создаем или обновляем операцию заказа
                        existing_order_op = db.query(ProductionOperation).filter_by(
                            order_id=current_order.order_id,
                            operation_id=operation.operation_id
                        ).first()

                        if existing_order_op:
                            if (existing_order_op.planned_quantity != planned_quantity or
                                existing_order_op.time_norm != time_norm or
                                existing_order_op.standard_hours != standard_hours or
                                existing_order_op.stage_id != (stage.stage_id if stage else None)):
                                existing_order_op.planned_quantity = planned_quantity
                                existing_order_op.time_norm = time_norm
                                existing_order_op.standard_hours = standard_hours
                                existing_order_op.stage_id = stage.stage_id if stage else None
                                # Не увеличиваем счетчик, так как это обновление
                        else:
                            new_order_op = ProductionOperation(
                                order_id=current_order.order_id,
                                operation_id=operation.operation_id,
                                planned_quantity=planned_quantity,
                                time_norm=time_norm,
                                standard_hours=standard_hours,
                                stage_id=stage.stage_id if stage else None
                            )
                            db.add(new_order_op)
                            # Не увеличиваем счетчик, так как это создание

                    except Exception as e:
                        print(f"Ошибка обработки операции заказа {ref_key}: {e}")
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
        stats.components_created = components_created
        stats.components_updated = components_updated
        stats.operations_created = operations_created
        stats.operations_updated = operations_updated

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации заказов на производство: {e}")

    return asdict(stats)