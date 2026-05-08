from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import SupplierOrder, SupplierOrderItem, Supplier, Item
from ..schemas import ODataSyncRequest

ALLOWED_ORGANIZATION_NAMES = {"зсм", "ооо зсм"}


def _parse_1c_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y", "on", "истина", "да"):
            return True
        if v in ("false", "0", "no", "n", "off", "ложь", "нет"):
            return False
    return default


def _norm_guid(value: Any) -> str:
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if s.startswith("guid'") and s.endswith("'"):
        s = s[len("guid'") : -1].strip()
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1].strip()
    return s


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return default


def _extract_state_name(record: Dict[str, Any]) -> str:
    state_raw = record.get("СостояниеЗаказа")
    if isinstance(state_raw, dict):
        return str(
            state_raw.get("Description")
            or state_raw.get("Наименование")
            or state_raw.get("Name")
            or ""
        ).strip()
    if state_raw:
        return str(state_raw).strip()
    return str(record.get("СостояниеЗаказа_Name") or record.get("СостояниеЗаказа_Description") or "").strip()


def _normalize_organization_name(value: Any) -> str:
    normalized = str(value or "").strip().casefold().replace("ё", "е")
    for ch in ('"', "'", "«", "»", ".", ",", "(", ")"):
        normalized = normalized.replace(ch, " ")
    return " ".join(normalized.split())


def _extract_organization_name(record: Dict[str, Any], organization_names_by_key: Dict[str, str]) -> str:
    organization_raw = record.get("Организация")
    if isinstance(organization_raw, dict):
        name = str(
            organization_raw.get("Description")
            or organization_raw.get("Наименование")
            or organization_raw.get("Name")
            or ""
        ).strip()
        if name:
            return name
    elif organization_raw:
        return str(organization_raw).strip()

    name = str(record.get("Организация_Name") or record.get("Организация_Description") or "").strip()
    if name:
        return name

    organization_key = _norm_guid(record.get("Организация_Key"))
    return organization_names_by_key.get(organization_key, "") if organization_key else ""


def _is_allowed_organization(record: Dict[str, Any], organization_names_by_key: Dict[str, str]) -> bool:
    organization_key = _norm_guid(record.get("Организация_Key"))
    organization_name = _extract_organization_name(record, organization_names_by_key)
    if not organization_key and not organization_name:
        # Older test doubles / atypical OData schemas may not expose organization.
        # Do not drop those records silently.
        return True
    return _normalize_organization_name(organization_name) in ALLOWED_ORGANIZATION_NAMES


def _load_supplier_order_state_names(client: Any) -> Dict[str, str]:
    """
    1C returns order state in documents as GUID. Human-readable names live in a separate catalog.
    If the catalog is unavailable, sync should continue and keep GUIDs for diagnostics.
    """
    state_names: Dict[str, str] = {}
    try:
        rows = client.get_all(
            "Catalog_СостоянияЗаказовПоставщикам",
            select_fields=["Ref_Key", "Description"],
            top=1000,
            max_pages=10,
        )
    except Exception as e:
        print(f"Не удалось загрузить состояния заказов поставщику: {e}")
        return state_names

    for row in rows or []:
        key = _norm_guid(row.get("Ref_Key"))
        name = str(row.get("Description") or "").strip()
        if key and name:
            state_names[key] = name
    return state_names


def _load_organization_names(client: Any) -> Dict[str, str]:
    organization_names: Dict[str, str] = {}
    try:
        rows = client.get_all(
            "Catalog_Организации",
            select_fields=["Ref_Key", "Description"],
            top=1000,
            max_pages=10,
        )
    except Exception as e:
        print(f"Не удалось загрузить организации: {e}")
        return organization_names

    for row in rows or []:
        key = _norm_guid(row.get("Ref_Key"))
        name = str(row.get("Description") or "").strip()
        if key and name:
            organization_names[key] = name
    return organization_names


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
    orders_skipped_by_organization: int = 0
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
        state_names_by_key = _load_supplier_order_state_names(client)
        organization_names_by_key = _load_organization_names(client)

        orders_select_default = [
            "Ref_Key",
            "Number",
            "Date",
            "Posted",
            "DeletionMark",
            "СостояниеЗаказа_Key",
            "Организация_Key",
            "Контрагент_Key",
            "СуммаДокумента",
            "Запасы",
        ]
        effective_select_fields = list(req.select_fields or orders_select_default)
        for required_field in orders_select_default:
            if required_field not in effective_select_fields:
                effective_select_fields.append(required_field)

        # Получаем все записи заказов поставщикам
        order_data = client.get_all(
            req.entity_name,
            filter_query=req.filter_query,
            select_fields=effective_select_fields,
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
        skipped_by_organization = 0

        # Обрабатываем каждый заказ
        for record in order_data:
            ref_key = ''
            try:
                ref_key = record.get('Ref_Key', '').strip()
                if not ref_key:
                    continue

                existing_order = existing_orders.get(ref_key)
                if not _is_allowed_organization(record, organization_names_by_key):
                    skipped_by_organization += 1
                    if existing_order and not bool(existing_order.deletion_mark):
                        existing_order.deletion_mark = True
                        updated_count += 1
                    continue

                # Извлекаем данные заказа
                number = record.get('Number', '').strip()
                date_str = record.get('Date', '')
                is_posted = _parse_1c_bool(record.get('Posted'), False)
                deletion_mark = _parse_1c_bool(record.get('DeletionMark'), False)
                order_state_key = _norm_guid(record.get('СостояниеЗаказа_Key')) or None
                order_state_name = _extract_state_name(record) or (
                    state_names_by_key.get(order_state_key or "") if order_state_key else ""
                ) or None
                supplier_key = record.get('Контрагент_Key', '').strip()
                document_amount = _to_float(record.get('СуммаДокумента'), 0.0)

                # Конвертируем дату
                order_date = _parse_datetime(date_str) or datetime.now()

                if not number:
                    continue

                # Обрабатываем позиции заказа
                items_data = record.get('Запасы', [])
                if not isinstance(items_data, list):
                    items_data = []

                current_order = existing_order

                if existing_order:
                    # Проверяем, нужно ли обновлять
                    needs_update = (
                        existing_order.order_number != number or
                        existing_order.order_date != order_date or
                        existing_order.is_posted != is_posted or
                        existing_order.document_amount != document_amount or
                        existing_order.order_state_key != order_state_key or
                        existing_order.order_state_name != order_state_name or
                        existing_order.deletion_mark != deletion_mark
                    )

                    if needs_update:
                        existing_order.order_number = number
                        existing_order.order_date = order_date
                        existing_order.is_posted = is_posted
                        existing_order.document_amount = document_amount
                        existing_order.order_state_key = order_state_key
                        existing_order.order_state_name = order_state_name
                        existing_order.deletion_mark = deletion_mark
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
                        is_posted=is_posted,
                        order_state_key=order_state_key,
                        order_state_name=order_state_name,
                        deletion_mark=deletion_mark,
                    )
                    db.add(current_order)
                    created_count += 1

                # Проверяем, что заказ создан или найден
                if not current_order:
                    continue

                # Обрабатываем поставщика
                if supplier_key:
                    supplier_raw = record.get('Контрагент') or {}
                    if isinstance(supplier_raw, dict):
                        supplier_name = str(supplier_raw.get('Description') or supplier_raw.get('Наименование') or '').strip()
                    else:
                        supplier_name = str(supplier_raw or '').strip()
                    existing_supplier = existing_suppliers.get(supplier_key)

                    if existing_supplier:
                        if existing_supplier.supplier_name != supplier_name:
                            existing_supplier.supplier_name = supplier_name
                            suppliers_updated += 1
                        current_order.supplier_id = existing_supplier.supplier_id
                    else:
                        new_supplier = Supplier(
                            supplier_ref1c=supplier_key,
                            supplier_name=supplier_name
                        )
                        db.add(new_supplier)
                        db.flush()
                        existing_suppliers[supplier_key] = new_supplier
                        suppliers_created += 1

                        # Связываем заказ с поставщиком
                        current_order.supplier_id = new_supplier.supplier_id
                else:
                    # Если поставщик не указан, устанавливаем supplier_id в None
                    current_order.supplier_id = None

                db.flush()

                # Обрабатываем позиции заказа
                seen_item_keys = set()
                for item_data in items_data:
                    try:
                        item_key = item_data.get('Номенклатура_Key', '').strip()
                        line_number_raw = item_data.get("LineNumber") or item_data.get("НомерСтроки")
                        try:
                            line_number = int(line_number_raw) if line_number_raw not in (None, "") else None
                        except Exception:
                            line_number = None
                        characteristic_ref1c = _norm_guid(
                            item_data.get("Характеристика_Key") or item_data.get("Characteristic_Key")
                        ) or None
                        quantity = _to_float(item_data.get('Количество'), 0.0)
                        received_qty = _to_float(
                            item_data.get("КоличествоПоступило")
                            or item_data.get("Получено")
                            or item_data.get("Поступило"),
                            0.0,
                        )
                        remaining_qty = max(quantity - received_qty, 0.0)
                        price = _to_float(item_data.get('Цена'), 0.0)
                        amount = _to_float(item_data.get('Сумма'), 0.0)
                        delivery_date_str = item_data.get('ДатаПоступления', '')

                        if not item_key:
                            continue

                        # Конвертируем дату поставки
                        delivery_date = _parse_datetime(delivery_date_str)

                        # Находим номенклатуру
                        item = existing_items.get(item_key)
                        if not item:
                            continue

                        seen_item_keys.add((line_number, item.item_id))

                        if line_number is not None:
                            existing_order_item = db.query(SupplierOrderItem).filter_by(
                                order_id=current_order.order_id,
                                line_number=line_number,
                            ).first()
                        else:
                            existing_order_item = db.query(SupplierOrderItem).filter_by(
                                order_id=current_order.order_id,
                                item_id_ref=item.item_id,
                                line_number=None,
                            ).first()

                        if existing_order_item:
                            if (existing_order_item.quantity != quantity or
                                existing_order_item.received_qty != received_qty or
                                existing_order_item.remaining_qty != remaining_qty or
                                existing_order_item.price != price or
                                existing_order_item.amount != amount or
                                existing_order_item.delivery_date != delivery_date or
                                existing_order_item.item_id_ref != item.item_id or
                                existing_order_item.characteristic_ref1c != characteristic_ref1c):
                                existing_order_item.item_id_ref = item.item_id
                                existing_order_item.line_number = line_number
                                existing_order_item.characteristic_ref1c = characteristic_ref1c
                                existing_order_item.quantity = quantity
                                existing_order_item.received_qty = received_qty
                                existing_order_item.remaining_qty = remaining_qty
                                existing_order_item.price = price
                                existing_order_item.amount = amount
                                existing_order_item.delivery_date = delivery_date
                                items_updated += 1
                        else:
                            new_order_item = SupplierOrderItem(
                                order_id=current_order.order_id,
                                item_id_ref=item.item_id,
                                line_number=line_number,
                                characteristic_ref1c=characteristic_ref1c,
                                quantity=quantity,
                                received_qty=received_qty,
                                remaining_qty=remaining_qty,
                                price=price,
                                amount=amount,
                                delivery_date=delivery_date
                            )
                            db.add(new_order_item)
                            items_created += 1

                    except Exception as e:
                        print(f"Ошибка обработки позиции заказа {ref_key}: {e}")
                        continue

                if items_data:
                    for existing_row in db.query(SupplierOrderItem).filter_by(order_id=current_order.order_id).all():
                        row_key = (existing_row.line_number, existing_row.item_id_ref)
                        if row_key not in seen_item_keys:
                            db.delete(existing_row)

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
        stats.orders_skipped_by_organization = skipped_by_organization

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации заказов поставщикам: {e}")

    return asdict(stats)
