from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import SupplierOrder, SupplierOrderItem, Supplier, Item
from ..schemas import ODataSyncRequest


@dataclass
class SupplierOrderSyncStats:
    """Статистика синхронизации заказов поставщикам"""
    orders_total: int = 0
    orders_created: int = 0
    orders_updated: int = 0
    orders_unchanged: int = 0
    items_created: int = 0
    items_updated: int = 0
    suppliers_created: int = 0
    suppliers_updated: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def sync_supplier_orders_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация заказов поставщикам из 1С через OData.

    Алгоритм:
    1. Загружаем все записи из Document_ЗаказПоставщику
    2. Для каждого заказа создаем или обновляем SupplierOrder
    3. Обрабатываем вложенные позиции заказа (SupplierOrderItem)
    4. Обрабатываем поставщиков (Supplier)
    5. Обновляем статистику синхронизации
    """
    from ..services.odata_client import OData1CClient

    stats = SupplierOrderSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    try:
        # Создаем клиент OData
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # Получаем все записи заказов поставщикам
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
        existing_orders = {order.order_ref1c: order for order in db.query(SupplierOrder).all() if order.order_ref1c}
        existing_suppliers = {sup.supplier_ref1c: sup for sup in db.query(Supplier).all() if sup.supplier_ref1c}
        existing_items = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        items_created = 0
        items_updated = 0
        suppliers_created = 0
        suppliers_updated = 0

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
                supplier_key = record.get('Контрагент_Key', '').strip()
                document_amount = record.get('СуммаДокумента', 0.0)

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

                # Обрабатываем позиции заказа
                items_data = record.get('Запасы', [])
                if not isinstance(items_data, list):
                    items_data = []

                # Проверяем, существует ли уже такой заказ
                existing_order = existing_orders.get(ref_key)
                current_order = existing_order

                if existing_order:
                    # Проверяем, нужно ли обновлять
                    needs_update = (
                        existing_order.order_number != number or
                        existing_order.order_date != order_date or
                        existing_order.is_posted != is_posted or
                        existing_order.document_amount != document_amount
                    )

                    if needs_update:
                        existing_order.order_number = number
                        existing_order.order_date = order_date
                        existing_order.is_posted = is_posted
                        existing_order.document_amount = document_amount
                        updated_count += 1
                    else:
                        unchanged_count += 1
                else:
                    # Создаем новый заказ
                    current_order = SupplierOrder(
                        order_number=number,
                        order_date=order_date,
                        order_ref1c=ref_key,
                        document_amount=document_amount,
                        is_posted=is_posted
                    )
                    db.add(current_order)
                    created_count += 1

                # Проверяем, что заказ создан или найден
                if not current_order:
                    continue

                # Обрабатываем поставщика
                if supplier_key:
                    supplier_name = record.get('Контрагент', {}).get('Description', '').strip()
                    existing_supplier = existing_suppliers.get(supplier_key)

                    if existing_supplier:
                        if existing_supplier.supplier_name != supplier_name:
                            existing_supplier.supplier_name = supplier_name
                            suppliers_updated += 1
                    else:
                        new_supplier = Supplier(
                            supplier_ref1c=supplier_key,
                            supplier_name=supplier_name
                        )
                        db.add(new_supplier)
                        suppliers_created += 1

                        # Связываем заказ с поставщиком
                        current_order.supplier_id = new_supplier.supplier_id
                else:
                    # Если поставщик не указан, устанавливаем supplier_id в None
                    current_order.supplier_id = None

                # Обрабатываем позиции заказа
                for item_data in items_data:
                    try:
                        item_key = item_data.get('Номенклатура_Key', '').strip()
                        quantity = item_data.get('Количество', 0.0)
                        price = item_data.get('Цена', 0.0)
                        amount = item_data.get('Сумма', 0.0)
                        delivery_date_str = item_data.get('ДатаПоступления', '')

                        if not item_key:
                            continue

                        # Конвертируем дату поставки
                        delivery_date = None
                        if delivery_date_str:
                            try:
                                if isinstance(delivery_date_str, str):
                                    delivery_date = datetime.fromisoformat(delivery_date_str.replace('Z', '+00:00'))
                                else:
                                    delivery_date = delivery_date_str
                            except:
                                delivery_date = None

                        # Находим номенклатуру
                        item = existing_items.get(item_key)
                        if not item:
                            continue

                        # Создаем или обновляем позицию заказа
                        existing_order_item = db.query(SupplierOrderItem).filter_by(
                            order_id=current_order.order_id,
                            item_id=item.item_id
                        ).first()

                        if existing_order_item:
                            if (existing_order_item.quantity != quantity or
                                existing_order_item.price != price or
                                existing_order_item.amount != amount or
                                existing_order_item.delivery_date != delivery_date):
                                existing_order_item.quantity = quantity
                                existing_order_item.price = price
                                existing_order_item.amount = amount
                                existing_order_item.delivery_date = delivery_date
                                items_updated += 1
                        else:
                            new_order_item = SupplierOrderItem(
                                order_id=current_order.order_id,
                                item_id=item.item_id,
                                quantity=quantity,
                                price=price,
                                amount=amount,
                                delivery_date=delivery_date
                            )
                            db.add(new_order_item)
                            items_created += 1

                    except Exception as e:
                        print(f"Ошибка обработки позиции заказа {ref_key}: {e}")
                        continue

            except Exception as e:
                # Логируем ошибку, но продолжаем обработку
                print(f"Ошибка обработки заказа поставщику {ref_key}: {e}")
                continue

        # Сохраняем изменения
        stats.orders_created = created_count
        stats.orders_updated = updated_count
        stats.orders_unchanged = unchanged_count
        stats.items_created = items_created
        stats.items_updated = items_updated
        stats.suppliers_created = suppliers_created
        stats.suppliers_updated = suppliers_updated

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации заказов поставщикам: {e}")

    return asdict(stats)