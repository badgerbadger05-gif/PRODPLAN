"""Read exact 1C documents into normalized supplier receipt evidence.

This is deliberately a read-only boundary.  It never searches the stock
register by ``Recorder`` (that query is unreliable on the live 1C endpoint)
and it never persists raw OData payloads.  Every requested recorder is fetched
once by its document key and reconciled to the already-visible Ledger rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

from sqlalchemy.orm import Session

from app import models
from app.services.odata_client import OData1CClient

from .supplier_receipt_allocation import (
    CORRECTION_OPERATION,
    RECEIPT_OPERATION,
    SUPPLIER_ORDER_TYPE,
    SUPPLIER_RETURN_OPERATION,
    TRANSFER_OPERATION,
    SupplierDocumentEvidence,
    SupplierReceiptEvidenceError,
    _operation_prefix,
)


_SUPPORTED_DOCUMENTS = {
    "Document_ПриходнаяНакладная": RECEIPT_OPERATION,
    "Document_КорректировкаПоступления": CORRECTION_OPERATION,
    "Document_РасходнаяНакладная": SUPPLIER_RETURN_OPERATION,
    "Document_ПеремещениеЗапасов": TRANSFER_OPERATION,
}
_TABLE_PART = "Запасы"
_ZERO_GUID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class SupplierEvidenceDiagnostic:
    recorder_type: str
    recorder_ref: str
    line_no: str
    code: str
    detail: str


@dataclass(frozen=True)
class SupplierEvidenceExtractionResult:
    evidence: tuple[SupplierDocumentEvidence, ...]
    diagnostics: tuple[SupplierEvidenceDiagnostic, ...]
    fetched_document_count: int


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalized_type(value: object) -> str:
    result = _text(value)
    for prefix in ("StandardODATA.", "StandardODATA/"):
        if result.startswith(prefix):
            return result[len(prefix):]
    return result


def _guid(value: object) -> str:
    result = _text(value).lower()
    return "" if result == _ZERO_GUID else result


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _first(row: dict, *names: str) -> object:
    for name in names:
        value = row.get(name)
        if value is not None and _text(value):
            return value
    return ""


def _operation(doc: dict) -> tuple[str, str]:
    return (
        _guid(_first(doc, "ХозяйственнаяОперация_Key", "ВидОперации_Key")),
        _text(_first(doc, "ВидОперации", "ХозяйственнаяОперация")),
    )


def _order(row: dict, doc: dict) -> tuple[str, str, str]:
    ref = _guid(_first(row, "Заказ", "Заказ_Key")) or _guid(
        _first(doc, "Заказ", "Заказ_Key")
    )
    order_type = _text(_first(row, "Заказ_Type")) or _text(
        _first(doc, "Заказ_Type")
    )
    # Live incoming documents often identify only the order document, not its
    # table-part line.  Zero is meaningful evidence and is resolved later only
    # when the canonical order item is unique.
    line = _text(_first(
        row,
        "НомерСтрокиДокументаОснования",
        "СтрокаЗаказа",
        "НомерСтрокиЗаказа",
    )) or "0"
    return ref, order_type, line


def _warehouse(
    recorder_type: str,
    signed_qty: Decimal,
    row: dict,
    doc: dict,
) -> str:
    if recorder_type == "Document_ПеремещениеЗапасов":
        names = (
            ("СкладПолучатель_Key", "СтруктурнаяЕдиницаПолучатель_Key")
            if signed_qty > 0
            else ("СкладОтправитель_Key", "СтруктурнаяЕдиница_Key")
        )
        return _guid(_first(row, *names)) or _guid(_first(doc, *names))
    names = ("Склад_Key", "СтруктурнаяЕдиница_Key")
    return _guid(_first(row, *names)) or _guid(_first(doc, *names))


def _correction_origin(doc: dict) -> tuple[str, str]:
    ref = _guid(_first(
        doc,
        "ИсправляемыйДокументПоступления",
        "ИсправляемыйДокументПоступления_Key",
        "ДокументПоступления_Key",
        "КорректируемыйДокумент",
        "КорректируемыйДокумент_Key",
        "ДокументОснование",
        "ДокументОснование_Key",
    ))
    doc_type = _normalized_type(_first(
        doc,
        "ИсправляемыйДокументПоступления_Type",
        "ДокументПоступления_Type",
        "КорректируемыйДокумент_Type",
        "ДокументОснование_Type",
    ))
    return ref, doc_type


def _correction_delta(row: dict) -> Decimal | None:
    """Return a verifiable new-minus-old quantity from a correction row."""
    pairs = (
        ("КоличествоДоИзменения", "Количество"),
        ("КоличествоДоКорректировки", "Количество"),
        ("КоличествоДоИзменения", "КоличествоПослеИзменения"),
        ("КоличествоСтарое", "КоличествоНовое"),
        ("КоличествоДо", "КоличествоПосле"),
        ("СтароеКоличество", "НовоеКоличество"),
    )
    for old_name, new_name in pairs:
        if old_name not in row or new_name not in row:
            continue
        old = _decimal(row.get(old_name))
        new = _decimal(row.get(new_name))
        if old is not None and new is not None:
            return new - old
    return None


def _strict_operation_matches(expected: str, key: str, name: str) -> bool:
    probe = SupplierDocumentEvidence(
        receipt_doc_type="",
        receipt_doc_ref="",
        receipt_doc_line_no="",
        operation_key=key,
        operation_name=name,
        supplier_order_type="",
        supplier_order_ref="",
        supplier_order_line_no="0",
        item_id=0,
        characteristic_ref="",
        warehouse_ref1c="",
        signed_qty=Decimal("0"),
        correction_receipt_ref="probe" if expected == CORRECTION_OPERATION else None,
    )
    try:
        return _operation_prefix(probe) == expected
    except SupplierReceiptEvidenceError:
        return False


def _diagnostic(
    entry: models.StockLedgerEntry,
    code: str,
    detail: str,
) -> SupplierEvidenceDiagnostic:
    return SupplierEvidenceDiagnostic(
        recorder_type=_text(entry.recorder_type),
        recorder_ref=_text(entry.recorder_ref),
        line_no=_text(entry.line_no),
        code=code,
        detail=detail,
    )


def extract_supplier_document_evidence(
    db: Session,
    client: OData1CClient,
    visible_entries: Iterable[models.StockLedgerEntry],
) -> SupplierEvidenceExtractionResult:
    """Fetch exact recorder documents and fail closed on every mismatch."""

    entries = sorted(
        tuple(visible_entries),
        key=lambda row: (
            _text(row.recorder_type),
            _text(row.recorder_ref),
            _text(row.line_no),
            _text(row.warehouse_ref1c),
            int(row.id or 0),
        ),
    )
    item_ids = {int(row.item_id) for row in entries}
    item_refs = {
        int(item_id): _guid(item_ref)
        for item_id, item_ref in db.query(
            models.Item.item_id, models.Item.item_ref1c
        ).filter(models.Item.item_id.in_(item_ids)).all()
    } if item_ids else {}

    grouped: dict[tuple[str, str], list[models.StockLedgerEntry]] = {}
    for entry in entries:
        grouped.setdefault(
            (_text(entry.recorder_type), _text(entry.recorder_ref)), []
        ).append(entry)

    evidence: list[SupplierDocumentEvidence] = []
    diagnostics: list[SupplierEvidenceDiagnostic] = []
    fetched = 0
    for (recorder_type, recorder_ref), document_entries in sorted(grouped.items()):
        expected_operation = _SUPPORTED_DOCUMENTS.get(recorder_type)
        if expected_operation is None:
            diagnostics.extend(
                _diagnostic(row, "unsupported_document_type", recorder_type)
                for row in document_entries
            )
            continue
        if not recorder_ref:
            diagnostics.extend(
                _diagnostic(row, "missing_recorder_ref", "recorder ref is empty")
                for row in document_entries
            )
            continue

        endpoint = f"{recorder_type}(guid'{recorder_ref}')"
        try:
            raw = client._make_request(endpoint)
            table_rows = client.get_all(
                f"{recorder_type}_{_TABLE_PART}",
                filter_query=f"Ref_Key eq guid'{recorder_ref}'",
                top=1000,
                max_pages=100,
                order_by="LineNumber",
            )
            fetched += 1
        except Exception as exc:
            diagnostics.extend(
                _diagnostic(
                    row,
                    "document_fetch_failed",
                    f"{type(exc).__name__}: exact document unavailable",
                )
                for row in document_entries
            )
            continue
        doc = raw if isinstance(raw, dict) else {}
        if _guid(doc.get("Ref_Key")) != _guid(recorder_ref):
            diagnostics.extend(
                _diagnostic(row, "wrong_document_ref", "returned Ref_Key differs")
                for row in document_entries
            )
            continue

        operation_key, operation_name = _operation(doc)
        if not _strict_operation_matches(
            expected_operation, operation_key, operation_name
        ):
            diagnostics.extend(
                _diagnostic(
                    row, "wrong_operation",
                    "document operation is missing or unsupported for its type",
                )
                for row in document_entries
            )
            continue

        correction_ref: str | None = None
        if recorder_type == "Document_КорректировкаПоступления":
            origin_ref, origin_type = _correction_origin(doc)
            if (
                not origin_ref
                or origin_type != "Document_ПриходнаяНакладная"
            ):
                diagnostics.extend(
                    _diagnostic(
                        row,
                        "missing_original_receipt",
                        "correction has no typed incoming-receipt basis",
                    )
                    for row in document_entries
                )
                continue
            correction_ref = origin_ref

        rows = table_rows
        if not isinstance(rows, list):
            diagnostics.extend(
                _diagnostic(row, "missing_table_part", "Запасы is not a list")
                for row in document_entries
            )
            continue
        by_line: dict[str, list[dict]] = {}
        for raw_row in rows:
            if isinstance(raw_row, dict):
                by_line.setdefault(_text(raw_row.get("LineNumber")), []).append(raw_row)

        entry_units: list[
            tuple[models.StockLedgerEntry, list[models.StockLedgerEntry], Decimal]
        ] = []
        if recorder_type == "Document_ПеремещениеЗапасов":
            entry_units = [
                (entry, [entry], Decimal(str(entry.qty)))
                for entry in document_entries
            ]
        else:
            by_sle_line: dict[str, list[models.StockLedgerEntry]] = {}
            for entry in document_entries:
                by_sle_line.setdefault(_text(entry.line_no), []).append(entry)
            for line_entries in by_sle_line.values():
                representative = line_entries[0]
                entry_units.append((
                    representative,
                    line_entries,
                    sum(
                        (Decimal(str(row.qty)) for row in line_entries),
                        Decimal("0"),
                    ),
                ))

        for entry, physical_rows, sle_qty in entry_units:
            line_rows = by_line.get(_text(entry.line_no), [])
            if len(line_rows) != 1:
                code = "missing_document_line" if not line_rows else "duplicate_document_line"
                diagnostics.append(_diagnostic(
                    entry, code, "exactly one table-part row is required"
                ))
                continue
            row = line_rows[0]
            document_item_ref = _guid(row.get("Номенклатура_Key"))
            expected_item_ref = item_refs.get(int(entry.item_id), "")
            if not expected_item_ref:
                diagnostics.append(_diagnostic(
                    entry, "missing_item_identity", "Ledger item has no 1C identity"
                ))
                continue
            if document_item_ref != expected_item_ref:
                diagnostics.append(_diagnostic(
                    entry, "item_mismatch", "document item differs from Ledger row"
                ))
                continue
            characteristic = _guid(_first(
                row, "Характеристика_Key", "Characteristic_Key"
            ))
            if (
                any(int(physical.item_id) != int(entry.item_id) for physical in physical_rows)
                or any(
                    _guid(physical.characteristic_ref) != characteristic
                    for physical in physical_rows
                )
            ):
                diagnostics.append(_diagnostic(
                    entry,
                    "characteristic_mismatch",
                    "document characteristic differs from Ledger row",
                ))
                continue
            raw_qty = _decimal(row.get("Количество"))
            if recorder_type == "Document_КорректировкаПоступления":
                signed_qty = sle_qty
                delta = _correction_delta(row)
                if delta is None:
                    diagnostics.append(_diagnostic(
                        entry,
                        "correction_delta_unverified",
                        "correction row has no supported before/after quantity fields",
                    ))
                    continue
                if delta != sle_qty:
                    diagnostics.append(_diagnostic(
                        entry, "quantity_mismatch",
                        "correction new-minus-old differs from Ledger row",
                    ))
                    continue
            elif raw_qty is None:
                diagnostics.append(_diagnostic(
                    entry, "missing_quantity", "document quantity is not decimal"
                ))
                continue
            elif recorder_type == "Document_ПриходнаяНакладная":
                signed_qty = abs(raw_qty)
            elif recorder_type == "Document_РасходнаяНакладная":
                signed_qty = -abs(raw_qty)
            else:
                signed_qty = abs(raw_qty) if sle_qty > 0 else -abs(raw_qty)
            if signed_qty != sle_qty:
                diagnostics.append(_diagnostic(
                    entry, "quantity_mismatch",
                    "signed document quantity differs from Ledger row",
                ))
                continue
            warehouse = _warehouse(recorder_type, signed_qty, row, doc)
            if (
                not warehouse
                or any(
                    warehouse != _guid(physical.warehouse_ref1c)
                    for physical in physical_rows
                )
            ):
                diagnostics.append(_diagnostic(
                    entry, "warehouse_mismatch",
                    "document warehouse differs from Ledger row",
                ))
                continue
            order_ref, order_type, order_line = _order(row, doc)
            if (
                recorder_type != "Document_ПеремещениеЗапасов"
                and (
                    _normalized_type(order_type) != SUPPLIER_ORDER_TYPE
                    or not order_ref
                )
            ):
                # This is a valid physical supplier fact with no addressable
                # frozen-order lineage (direct receipt/surplus).  Preserve it
                # as normalized evidence; the generation-scoped provenance
                # layer classifies it as explicitly unplanned.
                order_ref = ""
                order_type = ""
                order_line = "0"
            evidence.append(SupplierDocumentEvidence(
                receipt_doc_type=recorder_type,
                receipt_doc_ref=recorder_ref,
                receipt_doc_line_no=_text(entry.line_no),
                operation_key=operation_key,
                operation_name=operation_name,
                supplier_order_type=order_type,
                supplier_order_ref=order_ref,
                supplier_order_line_no=order_line,
                item_id=int(entry.item_id),
                characteristic_ref=characteristic,
                warehouse_ref1c=warehouse,
                signed_qty=signed_qty,
                correction_receipt_ref=correction_ref,
            ))

    return SupplierEvidenceExtractionResult(
        evidence=tuple(evidence),
        diagnostics=tuple(diagnostics),
        fetched_document_count=fetched,
    )
