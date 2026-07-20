from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional, List, Tuple
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import SupplierOrder, SupplierOrderItem, Supplier, Item
from ..schemas import ODataSyncRequest

ALLOWED_ORGANIZATION_NAMES = {"зсм", "ооо зсм"}


def _parse_1c_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, dict):
        for key in ("value", "Value", "val", "boolean", "Boolean"):
            if key in value:
                return _parse_1c_bool(value.get(key), default)
        return default
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y", "on", "истина", "да"):
            return True
        if v in ("false", "0", "no", "n", "off", "ложь", "нет"):
            return False
    return default


from app.utils.guid import norm_guid as _norm_guid


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


from app.utils.numeric import to_float as _to_float


def _is_zero_guid(value: Any) -> bool:
    return _norm_guid(value) == "00000000-0000-0000-0000-000000000000"


def _nonzero_guid(value: Any) -> Optional[str]:
    normalized = _norm_guid(value)
    if not normalized or _is_zero_guid(normalized):
        return None
    return normalized


def _resolve_supplier_destination(
    item_row: Dict[str, Any], order_row: Dict[str, Any]
) -> tuple[Optional[str], str]:
    """Resolve reserve destination without using generic structural-unit keys."""
    line_ref = _nonzero_guid(item_row.get("СтруктурнаяЕдиницаРезерв_Key"))
    if line_ref:
        return line_ref, "line"
    if _parse_1c_bool(order_row.get("УчетПотребностиПоСкладам"), False):
        header_ref = _nonzero_guid(order_row.get("СтруктурнаяЕдиницаРезерв_Key"))
        if header_ref:
            return header_ref, "header"
    return None, "unresolved"


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


def _extract_operation_kind(record: Dict[str, Any]) -> Optional[str]:
    value = record.get("ВидОперации")
    if isinstance(value, dict):
        value = (
            value.get("value")
            or value.get("Value")
            or value.get("Description")
            or value.get("Name")
        )
    normalized = str(value or "").strip()
    return normalized or None


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


def _load_supplier_names(client: Any, supplier_refs: set[str]) -> Dict[str, str]:
    supplier_names: Dict[str, str] = {}
    refs = sorted({str(ref or "").strip() for ref in supplier_refs if str(ref or "").strip()})
    if not refs:
        return supplier_names

    chunk_size = 20
    for start in range(0, len(refs), chunk_size):
        chunk = refs[start : start + chunk_size]
        filter_query = " or ".join(f"Ref_Key eq guid'{ref}'" for ref in chunk)
        try:
            rows = client.get_all(
                "Catalog_Контрагенты",
                filter_query=filter_query,
                select_fields=["Ref_Key", "Description"],
                top=1000,
                max_pages=5,
            )
        except Exception as e:
            print(f"Не удалось загрузить контрагентов: {e}")
            continue
        for row in rows or []:
            key = _norm_guid(row.get("Ref_Key"))
            name = str(row.get("Description") or "").strip()
            if key and name:
                supplier_names[key] = name
    return supplier_names


ReceiptKey = Tuple[str, str, str]

PROCESSING_TRANSFER_OPERATION_KEY = "8d970138-9934-11eb-e39a-fa163e61326a"
PROCESSING_REPORT_OPERATION_KEY = "8d96ffe4-9934-11eb-e39a-fa163e61326a"
PROCESSING_ORDER_OPERATION_KEY = "8d96f6a2-9934-11eb-e39a-fa163e61326a"


def _is_supplier_order_type(value: Any) -> bool:
    return "ЗаказПоставщику" in str(value or "")


@dataclass
class ProcessingDocumentLoadResult:
    transfer_available: bool
    report_available: bool
    transfer_dates: Dict[str, datetime]
    report_dates: Dict[str, datetime]
    report_qty: Dict[ReceiptKey, float]


def _load_processing_document_dates(
    client: Any, known_order_refs: set[str]
) -> ProcessingDocumentLoadResult:
    """Load the first material transfer and latest contractor report per order.

    Availability is tracked independently: a report failure must not discard a
    successfully loaded transfer date (and vice versa).
    """
    known = {_norm_guid(ref) for ref in known_order_refs if _norm_guid(ref)}
    if not known:
        return ProcessingDocumentLoadResult(True, True, {}, {}, {})
    try:
        transfers = client.get_all(
            "Document_РасходнаяНакладная",
            filter_query=(
                "Posted eq true and DeletionMark eq false and "
                f"ХозяйственнаяОперация_Key eq guid'{PROCESSING_TRANSFER_OPERATION_KEY}'"
            ),
            select_fields=[
                "Ref_Key",
                "Date",
                "Posted",
                "DeletionMark",
                "ХозяйственнаяОперация_Key",
                "ДокументОснование",
                "ДокументОснование_Type",
                "Заказ",
                "Заказ_Type",
            ],
            top=1000,
            max_pages=1000,
            order_by="Date",
        )
        transfer_available = True
    except Exception as e:
        print(f"Не удалось загрузить передачи в переработку: {e}")
        transfers = []
        transfer_available = False
    try:
        reports = client.get_all(
            "Document_ОтчетПереработчика",
            filter_query=(
                "Posted eq true and DeletionMark eq false and "
                f"ХозяйственнаяОперация_Key eq guid'{PROCESSING_REPORT_OPERATION_KEY}'"
            ),
            select_fields=[
                "Ref_Key",
                "Date",
                "Posted",
                "DeletionMark",
                "ХозяйственнаяОперация_Key",
                "ДокументОснование_Key",
                "Продукция",
            ],
            top=1000,
            max_pages=1000,
            order_by="Date",
        )
        report_available = True
    except Exception as e:
        print(f"Не удалось загрузить отчеты переработчика: {e}")
        reports = []
        report_available = False

    first_transfer: Dict[str, datetime] = {}
    for doc in transfers or []:
        if (
            not _parse_1c_bool(doc.get("Posted"), False)
            or _parse_1c_bool(doc.get("DeletionMark"), False)
            or _norm_guid(doc.get("ХозяйственнаяОперация_Key"))
            != PROCESSING_TRANSFER_OPERATION_KEY
        ):
            continue
        basis_ref = _norm_guid(doc.get("ДокументОснование"))
        basis_type = doc.get("ДокументОснование_Type")
        if not basis_ref:
            basis_ref = _norm_guid(doc.get("Заказ"))
            basis_type = doc.get("Заказ_Type")
        if (
            basis_ref not in known
            or not _is_supplier_order_type(basis_type)
        ):
            continue
        value = _parse_datetime(doc.get("Date"))
        if value is not None and (
            basis_ref not in first_transfer or value < first_transfer[basis_ref]
        ):
            first_transfer[basis_ref] = value

    latest_report: Dict[str, datetime] = {}
    report_qty: Dict[ReceiptKey, float] = defaultdict(float)
    for doc in reports or []:
        if (
            not _parse_1c_bool(doc.get("Posted"), False)
            or _parse_1c_bool(doc.get("DeletionMark"), False)
            or _norm_guid(doc.get("ХозяйственнаяОперация_Key"))
            != PROCESSING_REPORT_OPERATION_KEY
        ):
            continue
        basis_ref = _norm_guid(doc.get("ДокументОснование_Key"))
        if basis_ref not in known:
            continue
        products = doc.get("Продукция") or []
        if not isinstance(products, list):
            continue
        valid_product_found = False
        for row in products:
            if not isinstance(row, dict):
                continue
            item_ref = _norm_guid(row.get("Номенклатура_Key"))
            characteristic_ref = _norm_guid(
                row.get("Характеристика_Key") or row.get("Characteristic_Key")
            )
            quantity = _to_float(row.get("Количество"), 0.0)
            if item_ref and quantity > 0:
                valid_product_found = True
                report_qty[(basis_ref, item_ref, characteristic_ref)] += quantity
        if not valid_product_found:
            continue
        value = _parse_datetime(doc.get("Date"))
        if value is not None and (
            basis_ref not in latest_report or value > latest_report[basis_ref]
        ):
            latest_report[basis_ref] = value
    return ProcessingDocumentLoadResult(
        transfer_available,
        report_available,
        first_transfer,
        latest_report,
        dict(report_qty),
    )


def _load_receipts_by_supplier_order(client: Any, known_order_refs: set[str]) -> Dict[ReceiptKey, float]:
    """
    Posted incoming invoices carry the actual receipt against supplier orders.
    In UNF the supplier order is a polymorphic string field (`Заказ`) on the
    invoice header and usually repeated on each stock line.
    """
    known = {_norm_guid(ref) for ref in known_order_refs if _norm_guid(ref)}
    if not known:
        return {}

    try:
        receipt_docs = client.get_all(
            "Document_ПриходнаяНакладная",
            filter_query="Posted eq true and DeletionMark eq false",
            select_fields=[
                "Ref_Key",
                "Posted",
                "DeletionMark",
                "Заказ",
                "Заказ_Type",
                "Запасы",
            ],
            top=1000,
            max_pages=1000,
            order_by="Ref_Key",
        )
    except Exception as e:
        print(f"Не удалось загрузить приходные накладные для заказов поставщику: {e}")
        return {}

    receipts: Dict[ReceiptKey, float] = defaultdict(float)
    for doc in receipt_docs or []:
        if not _parse_1c_bool(doc.get("Posted"), False) or _parse_1c_bool(doc.get("DeletionMark"), False):
            continue
        header_order_ref = _norm_guid(doc.get("Заказ"))
        header_order_type = str(doc.get("Заказ_Type") or "")
        header_is_supplier_order = "ЗаказПоставщику" in header_order_type or not header_order_type
        rows = doc.get("Запасы") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_order_ref = _norm_guid(row.get("Заказ")) or header_order_ref
            if not row_order_ref or _is_zero_guid(row_order_ref) or row_order_ref not in known:
                continue
            row_order_type = str(row.get("Заказ_Type") or header_order_type or "")
            if row_order_type and "ЗаказПоставщику" not in row_order_type:
                continue
            if not row_order_type and not header_is_supplier_order:
                continue
            item_key = _norm_guid(row.get("Номенклатура_Key"))
            if not item_key:
                continue
            characteristic_ref1c = _norm_guid(
                row.get("Характеристика_Key") or row.get("Characteristic_Key")
            )
            qty = _to_float(row.get("Количество"), 0.0)
            if qty <= 0:
                continue
            receipts[(row_order_ref, item_key, characteristic_ref1c)] += qty
    return dict(receipts)


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
    receipt_rows_applied: int = 0
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
            "ВидОперации",
            "ХозяйственнаяОперация_Key",
            "СостояниеЗаказа_Key",
            "Организация_Key",
            "Контрагент_Key",
            "СуммаДокумента",
            "СтруктурнаяЕдиницаРезерв_Key",
            "УчетПотребностиПоСкладам",
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
        receipt_remaining_by_order_item = _load_receipts_by_supplier_order(
            client,
            {
                str(record.get("Ref_Key") or "").strip()
                for record in order_data
                if str(record.get("Ref_Key") or "").strip()
            },
        )
        processing_dates = _load_processing_document_dates(
            client,
            {
                str(record.get("Ref_Key") or "").strip()
                for record in order_data
                if str(record.get("Ref_Key") or "").strip()
            },
        )
        processing_transfer_dates = processing_dates.transfer_dates
        processing_report_dates = processing_dates.report_dates
        processing_report_remaining_by_order_item = dict(processing_dates.report_qty)

        # Получаем существующие записи для сопоставления
        existing_orders = {order.order_ref1c: order for order in db.query(SupplierOrder).all() if order.order_ref1c}
        existing_suppliers = {sup.supplier_ref1c: sup for sup in db.query(Supplier).all() if sup.supplier_ref1c}
        existing_items = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}
        supplier_names_by_key = _load_supplier_names(
            client,
            {
                str(record.get("Контрагент_Key") or "").strip()
                for record in order_data
                if str(record.get("Контрагент_Key") or "").strip()
            },
        )

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        items_created = 0
        items_updated = 0
        suppliers_created = 0
        suppliers_updated = 0
        skipped_by_organization = 0
        receipt_rows_applied = 0

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
                operation_name = _extract_operation_kind(record)
                operation_key = _nonzero_guid(
                    record.get("ХозяйственнаяОперация_Key")
                )
                normalized_order_ref = _norm_guid(ref_key)
                processing_transfer_date = processing_transfer_dates.get(
                    normalized_order_ref
                )
                processing_report_date = processing_report_dates.get(
                    normalized_order_ref
                )
                is_processing_order = (
                    operation_key == PROCESSING_ORDER_OPERATION_KEY
                    or str(operation_name or "").strip().casefold()
                    == "заказнапереработку"
                )
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
                        existing_order.operation_name != operation_name or
                        existing_order.operation_key != operation_key or
                        (
                            processing_dates.transfer_available
                            and existing_order.processing_transfer_date
                            != processing_transfer_date
                        ) or
                        (
                            processing_dates.report_available
                            and existing_order.processing_report_date
                            != processing_report_date
                        ) or
                        existing_order.order_state_key != order_state_key or
                        existing_order.order_state_name != order_state_name or
                        existing_order.deletion_mark != deletion_mark
                    )

                    if needs_update:
                        existing_order.order_number = number
                        existing_order.order_date = order_date
                        existing_order.is_posted = is_posted
                        existing_order.document_amount = document_amount
                        existing_order.operation_name = operation_name
                        existing_order.operation_key = operation_key
                        if processing_dates.transfer_available:
                            existing_order.processing_transfer_date = processing_transfer_date
                        if processing_dates.report_available:
                            existing_order.processing_report_date = processing_report_date
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
                        operation_name=operation_name,
                        operation_key=operation_key,
                        processing_transfer_date=processing_transfer_date,
                        processing_report_date=processing_report_date,
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
                    if not supplier_name:
                        supplier_name = supplier_names_by_key.get(supplier_key, "")
                    existing_supplier = existing_suppliers.get(supplier_key)

                    if existing_supplier:
                        if supplier_name and existing_supplier.supplier_name != supplier_name:
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
                        receipt_key = (_norm_guid(ref_key), _norm_guid(item_key), characteristic_ref1c or "")
                        if is_processing_order:
                            available_report_qty = (
                                processing_report_remaining_by_order_item.get(
                                    receipt_key, 0.0
                                )
                            )
                            received_qty = min(
                                max(quantity, 0.0), max(available_report_qty, 0.0)
                            )
                            processing_report_remaining_by_order_item[receipt_key] = max(
                                available_report_qty - received_qty, 0.0
                            )
                        else:
                            received_qty = _to_float(
                                item_data.get("КоличествоПоступило")
                                or item_data.get("Получено")
                                or item_data.get("Поступило"),
                                0.0,
                            )
                            received_from_receipts = 0.0
                            if quantity > 0 and receipt_remaining_by_order_item.get(receipt_key, 0.0) > 0:
                                available_receipt_qty = receipt_remaining_by_order_item.get(receipt_key, 0.0)
                                received_from_receipts = min(quantity, available_receipt_qty)
                                receipt_remaining_by_order_item[receipt_key] = max(
                                    available_receipt_qty - received_from_receipts,
                                    0.0,
                                )
                            if received_from_receipts > received_qty:
                                received_qty = received_from_receipts
                                receipt_rows_applied += 1
                        remaining_qty = max(quantity - received_qty, 0.0)
                        price = _to_float(item_data.get('Цена'), 0.0)
                        amount = _to_float(item_data.get('Сумма'), 0.0)
                        delivery_date_str = item_data.get('ДатаПоступления', '')
                        destination_ref, _destination_source = _resolve_supplier_destination(
                            item_data, record
                        )

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

                        if (
                            is_processing_order
                            and not processing_dates.report_available
                            and existing_order_item is not None
                        ):
                            # A report endpoint failure is unknown state, not
                            # proof of zero production. Preserve synchronized
                            # progress until reports are readable again, while
                            # keeping the quantity invariant if the order changed.
                            received_qty = min(
                                max(float(existing_order_item.received_qty or 0), 0.0),
                                max(quantity, 0.0),
                            )
                            remaining_qty = max(quantity - received_qty, 0.0)

                        if existing_order_item:
                            if (existing_order_item.quantity != quantity or
                                existing_order_item.received_qty != received_qty or
                                existing_order_item.remaining_qty != remaining_qty or
                                existing_order_item.price != price or
                                existing_order_item.amount != amount or
                                existing_order_item.delivery_date != delivery_date or
                                existing_order_item.item_id_ref != item.item_id or
                                existing_order_item.characteristic_ref1c != characteristic_ref1c or
                                existing_order_item.destination_warehouse_ref1c != destination_ref):
                                existing_order_item.item_id_ref = item.item_id
                                existing_order_item.line_number = line_number
                                existing_order_item.characteristic_ref1c = characteristic_ref1c
                                existing_order_item.quantity = quantity
                                existing_order_item.received_qty = received_qty
                                existing_order_item.remaining_qty = remaining_qty
                                existing_order_item.price = price
                                existing_order_item.amount = amount
                                existing_order_item.delivery_date = delivery_date
                                existing_order_item.destination_warehouse_ref1c = destination_ref
                                items_updated += 1
                        else:
                            new_order_item = SupplierOrderItem(
                                order_id=current_order.order_id,
                                item_id_ref=item.item_id,
                                line_number=line_number,
                                characteristic_ref1c=characteristic_ref1c,
                                destination_warehouse_ref1c=destination_ref,
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
        stats.receipt_rows_applied = receipt_rows_applied

        if req.dry_run:
            db.rollback()
        else:
            db.commit()
            # DBR feedback (Фаза 3): двигаем статусы закупных сигналов по факту
            # поступления материализованных заказов поставщику. Best-effort —
            # сбой обратной связи НЕ должен ронять синк (общий несущий сервис).
            try:
                from .dbr.feedback_service import apply_purchase_order_feedback

                fb_stats = apply_purchase_order_feedback(db)
                print(
                    f"[DBR PURCHASE FEEDBACK] signals_updated={fb_stats.get('signals_updated', 0)}"
                )
            except Exception as fb_exc:
                print(f"[DBR PURCHASE FEEDBACK WARNING] {fb_exc}")
                try:
                    db.rollback()
                except Exception:
                    pass

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации заказов поставщикам: {e}")

    return asdict(stats)
