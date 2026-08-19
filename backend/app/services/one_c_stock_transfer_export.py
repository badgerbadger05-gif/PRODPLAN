"""Export ProductionMaterialIssue documents to 1C as Document_ПеремещениеЗапасов.

Pattern: mirrors one_c_production_order_export.py.
Documentation: .docs/one_c_export_from_prodplan.md.

Safety per the doc:
1. Default dry_run=True (full preview, nothing is written).
2. Posted=false (proceedng stays on 1C admin side).
3. Idempotency via sync_link (source_doctype='material_issue').

A material issue in PRODPLAN models the warehouse-to-workshop transfer of
the components needed for one production_products line. In 1C this is a
Document_ПеремещениеЗапасов with СкладОтправитель_Key (source) and
СкладПолучатель_Key (destination), plus a Запасы table part with the
components.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from ..models import (
    Item,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SyncLink,
    Unit,
)
from .one_c_export_common import (
    DEFAULT_ORGANIZATION_REF1C,
    DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C,
    add_origin_marker as _add_origin_marker,
    add_unit_payload as _add_unit_payload,
    clean_ref1c as _clean_ref1c,
    config_ref1c as _config_ref1c,
    create_odata_client as _create_odata_client,
    current_1c_datetime as _current_1c_datetime,
    find_document_by_origin as _find_document_by_origin,
    find_sync_link as _find_sync_link,
    fmt_1c_datetime as _fmt_1c_datetime,
    origin_token as _origin_token,
    payload_hash as _payload_hash,
    post_export_entries as _post_export_entries,
    upsert_sync_link as _upsert_sync_link,
)
from .mrp_mutation_guard import require_materialized_orders
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient
from .one_c_document_numbers import material_issue_number
from .one_c_production_order_export import export_production_orders_to_1c
from .planning_truth import require_accepted_truth


STOCK_TRANSFER_ENTITY = "Document_ПеремещениеЗапасов"
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"
DEFAULT_STOCK_TRANSFER_VAT_RATE_REF1C = "4eae6f42-e295-11f0-9d39-9ee51454587f"


@dataclass
class StockTransferExportLine:
    line_number: int
    component_item_id: int
    item_ref1c: str
    item_name: str
    item_article: str
    unit_ref1c: Optional[str]
    qty: float


@dataclass
class StockTransferExportEntry:
    issue_id: int
    document_number: str
    product_id: int
    order_id: int
    order_ref1c: Optional[str]
    source_warehouse_ref1c: Optional[str]
    destination_warehouse_ref1c: Optional[str]
    lines: List[StockTransferExportLine] = field(default_factory=list)
    direction: str = "issue"
    product_item_ref1c: Optional[str] = None
    product_line_number: Optional[int] = None
    ledger_generation_id: Optional[int] = None
    document_date: Optional[datetime] = None
    origin_token: Optional[str] = None
    target_ref_key: Optional[str] = None
    status: str = "planned"
    error: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class StockTransferExportDefaults:
    organization_ref1c: str = ""
    source_structural_unit_ref1c: str = ""
    destination_structural_unit_ref1c: str = ""
    vat_rate_ref1c: str = ""


def _qty_from_balance_row(row: Dict[str, Any]) -> float:
    for key in ("КоличествоBalance", "КоличествоОстаток", "ВНаличииBalance", "ВНаличииОстаток"):
        if key in row:
            try:
                return float(row.get(key) or 0.0)
            except Exception:
                return 0.0
    return 0.0


def _stock_balance_cell_rows(
    client: OData1CClient,
    *,
    item_ref1c: str,
    warehouse_ref1c: str,
) -> List[Dict[str, Any]]:
    """
    Read live 1C balance by storage cell for a single item/warehouse pair.

    Some warehouses in UNF track stock by "Ячейка". A transfer row without
    Ячейка_Key can be created but cannot be conducted: 1C sees zero available
    stock in the empty-cell bucket. We intentionally query 1C at export/post
    time instead of relying on PRODPLAN's warehouse-only cache.
    """
    item_ref = _clean_ref1c(item_ref1c)
    warehouse_ref = _clean_ref1c(warehouse_ref1c)
    if not item_ref or not warehouse_ref:
        return []
    get_all = getattr(client, "get_all", None)
    if get_all is None:
        return []
    entity = (
        "AccumulationRegister_ЗапасыНаСкладах/Balance("
        f"Period=datetime'{_current_1c_datetime()}',"
        "Dimensions='Номенклатура,СтруктурнаяЕдиница,Ячейка,Организация')"
    )
    filter_query = (
        f"Номенклатура_Key eq guid'{item_ref}' and "
        f"СтруктурнаяЕдиница_Key eq guid'{warehouse_ref}'"
    )
    try:
        rows = get_all(
            entity,
            filter_query=filter_query,
            top=100,
            max_records=100,
            max_pages=5,
            order_by=None,
        )
    except Exception:
        return []
    useful: List[Dict[str, Any]] = []
    for row in rows or []:
        qty = _qty_from_balance_row(row)
        if qty <= 0:
            continue
        cell_ref = _clean_ref1c(row.get("Ячейка_Key"))
        if not cell_ref or cell_ref == EMPTY_REF1C:
            continue
        useful.append({"cell_ref1c": cell_ref, "qty": qty})
    useful.sort(key=lambda r: float(r.get("qty") or 0.0), reverse=True)
    return useful


def add_source_cells_to_payload(
    client: OData1CClient,
    entry: StockTransferExportEntry,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Fill/split transfer stock rows with Ячейка_Key from live 1C balances.

    The helper mutates and returns `payload`. Rows that do not require a
    storage cell stay as-is. If a required quantity is split across cells, the
    payload row is split accordingly with fresh LineNumber/КлючСвязи values.
    """
    source_ref = _clean_ref1c(payload.get("СтруктурнаяЕдиница_Key") or entry.source_warehouse_ref1c)
    if not source_ref:
        return payload

    rebuilt: List[Dict[str, Any]] = []
    for row in list(payload.get("Запасы") or []):
        item_ref = _clean_ref1c(row.get("Номенклатура_Key"))
        required_qty = float(row.get("Количество") or 0.0)
        if not item_ref or required_qty <= 0:
            rebuilt.append(row)
            continue

        cell_rows = _stock_balance_cell_rows(client, item_ref1c=item_ref, warehouse_ref1c=source_ref)
        if not cell_rows:
            rebuilt.append(row)
            continue

        single = next((r for r in cell_rows if float(r["qty"]) + 1e-9 >= required_qty), None)
        if single is not None:
            patched = dict(row)
            patched["Ячейка_Key"] = str(single["cell_ref1c"])
            rebuilt.append(patched)
            continue

        if sum(float(r["qty"]) for r in cell_rows) + 1e-9 < required_qty:
            patched = dict(row)
            patched["Ячейка_Key"] = str(cell_rows[0]["cell_ref1c"])
            rebuilt.append(patched)
            continue

        remaining = required_qty
        for cell in cell_rows:
            if remaining <= 1e-9:
                break
            qty = min(remaining, float(cell["qty"]))
            patched = dict(row)
            patched["Количество"] = qty
            patched["Ячейка_Key"] = str(cell["cell_ref1c"])
            rebuilt.append(patched)
            remaining -= qty

    for idx, row in enumerate(rebuilt, start=1):
        row["LineNumber"] = idx
        row["КлючСвязи"] = idx
    payload["Запасы"] = rebuilt
    if any(_clean_ref1c(row.get("Ячейка_Key")) for row in rebuilt):
        payload["ПоложениеЯчейкиОтправителя"] = "ВТабличнойЧасти"
    return payload


def _export_defaults(config: Dict[str, Any]) -> StockTransferExportDefaults:
    return StockTransferExportDefaults(
        organization_ref1c=_config_ref1c(
            config,
            "default_organization_ref1c",
            DEFAULT_ORGANIZATION_REF1C,
        ),
        source_structural_unit_ref1c=_config_ref1c(
            config,
            "default_transfer_source_structural_unit_ref1c",
        ),
        destination_structural_unit_ref1c=_config_ref1c(
            config,
            "default_transfer_destination_structural_unit_ref1c",
            DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C,
        ),
        vat_rate_ref1c=_config_ref1c(
            config,
            "default_stock_transfer_vat_rate_ref1c",
            DEFAULT_STOCK_TRANSFER_VAT_RATE_REF1C,
        ),
    )


def _existing_link(db: Session, issue_id: int) -> Optional[SyncLink]:
    return _find_sync_link(
        db,
        SyncLink,
        source_doctype="material_issue",
        source_id=int(issue_id),
        target_entity=STOCK_TRANSFER_ENTITY,
    )


def _entry_origin_token(entry: StockTransferExportEntry) -> str:
    """Durable cross-instance identity for one material transfer.

    Local issue/product ids are intentionally excluded: restored or cloned
    PRODPLAN databases can reuse them while writing to the same 1C base.
    """
    identity = {
        "basis_order_ref1c": _clean_ref1c(entry.order_ref1c),
        "product_item_ref1c": _clean_ref1c(entry.product_item_ref1c),
        "product_line_number": int(entry.product_line_number or 0),
        # One product may legitimately need a second transfer with the same
        # route and component quantities after the first issue is exported.
        # created_at is immutable business-occurrence data that survives a DB
        # backup/restore, unlike reusable local integer ids.
        "document_date": (
            entry.document_date.isoformat() if entry.document_date is not None else None
        ),
        "direction": str(entry.direction or "issue"),
        "source_warehouse_ref1c": _clean_ref1c(entry.source_warehouse_ref1c),
        "destination_warehouse_ref1c": _clean_ref1c(entry.destination_warehouse_ref1c),
        "lines": [
            {
                "item_ref1c": _clean_ref1c(line.item_ref1c),
                "qty": float(line.qty),
            }
            for line in sorted(entry.lines, key=lambda row: (row.item_ref1c, row.line_number))
        ],
    }
    return _origin_token("material_issue", identity)


def _origin_transfer_number(entry: StockTransferExportEntry, *, attempt: int = 0) -> str:
    if not entry.origin_token:
        raise ValueError("material issue export has no durable origin token")
    prefix = "RT" if str(entry.direction or "issue") == "return" else "MT"
    # 2 + 9 = the 11-character limit of Document_ПеремещениеЗапасов.Number.
    # Keep the familiar stable-contour format (MT000002982) for operators.
    # Linear probing is used only when that human-facing number is already
    # owned by another local or 1C document; durable ownership and retry
    # recovery continue to rely on prodplan-origin, not on the display number.
    number_part = (int(entry.issue_id) + int(attempt)) % 1_000_000_000
    return f"{prefix}{number_part:09d}"


def _document_by_number(client: Any, number: str) -> Optional[Dict[str, Any]]:
    get_all = getattr(client, "get_all", None)
    if get_all is None:
        return None
    escaped = str(number).replace("'", "''")
    rows = get_all(
        STOCK_TRANSFER_ENTITY,
        filter_query=f"Number eq '{escaped}'",
        select_fields=["Ref_Key", "Number", "Комментарий", "Posted", "DeletionMark"],
        top=2,
        max_records=2,
        max_pages=1,
        order_by=None,
    )
    if len(rows) > 1:
        raise RuntimeError(f"В 1С найдено несколько перемещений с номером {number}")
    return rows[0] if rows else None


def _document_by_ref(client: Any, ref_key: str) -> Optional[Dict[str, Any]]:
    get_all = getattr(client, "get_all", None)
    if get_all is None:
        return None
    ref = _clean_ref1c(ref_key)
    if not ref:
        return None
    rows = get_all(
        STOCK_TRANSFER_ENTITY,
        filter_query=f"Ref_Key eq guid'{ref}'",
        select_fields=["Ref_Key", "Number", "Комментарий", "Posted", "DeletionMark"],
        top=2,
        max_records=2,
        max_pages=1,
        order_by=None,
    )
    if len(rows) > 1:
        raise RuntimeError(f"В 1С найдено несколько перемещений с Ref_Key {ref}")
    return rows[0] if rows else None


def _owned_live_document(entry: StockTransferExportEntry, doc: Optional[Dict[str, Any]]) -> bool:
    if not doc or bool(doc.get("DeletionMark")):
        return False
    marker = f"prodplan-origin={entry.origin_token}"
    return marker in str(doc.get("Комментарий") or "")


def _allocate_transfer_number(
    db: Session,
    client: Any,
    entry: StockTransferExportEntry,
) -> tuple[str, Optional[Dict[str, Any]]]:
    marker = f"prodplan-origin={entry.origin_token}"
    for attempt in range(32):
        number = _origin_transfer_number(entry, attempt=attempt)
        local_owner = (
            db.query(ProductionMaterialIssue.issue_id)
            .filter(
                ProductionMaterialIssue.document_number == number,
                ProductionMaterialIssue.issue_id != int(entry.issue_id),
            )
            .first()
        )
        if local_owner is not None:
            continue
        remote = _document_by_number(client, number)
        if remote is None:
            return number, None
        if marker in str(remote.get("Комментарий") or "") and not bool(remote.get("DeletionMark")):
            return number, remote
    raise RuntimeError("Не удалось выделить уникальный номер перемещения в 1С")


def _guid_or_empty(value: Optional[str]) -> str:
    ref = _clean_ref1c(value)
    if not ref:
        return ""
    try:
        UUID(str(ref))
    except Exception:
        return ""
    return ref


def _resolve_unit_ref1c(db: Session, raw_unit: Optional[str]) -> Optional[str]:
    unit = str(raw_unit or "").strip()
    ref = _guid_or_empty(unit)
    if ref:
        return ref
    if not unit:
        return None
    row = (
        db.query(Unit)
        .filter(
            (Unit.unit_name == unit)
            | (Unit.short_name == unit)
            | (Unit.unit_full_name == unit)
            | (Unit.unit_code == unit)
        )
        .order_by(Unit.unit_id.asc())
        .first()
    )
    return _guid_or_empty(getattr(row, "unit_ref1c", None)) if row else None


def _collect_export_entries(
    db: Session, issue_ids: List[int]
) -> Tuple[List[StockTransferExportEntry], List[Dict[str, Any]]]:
    entries: List[StockTransferExportEntry] = []
    skipped: List[Dict[str, Any]] = []

    ids = [int(x) for x in issue_ids if x is not None]
    if not ids:
        return entries, skipped

    rows = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.lines).joinedload(
                ProductionMaterialIssueLine.component_item
            ),
            joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.order),
        )
        .filter(ProductionMaterialIssue.issue_id.in_(ids))
        .all()
    )
    found_ids = {int(i.issue_id) for i in rows}
    for missing in [x for x in ids if x not in found_ids]:
        skipped.append({"issue_id": missing, "reason": "ProductionMaterialIssue не найден"})

    for issue in rows:
        # 'cancelled' is a hard stop. 'exported' is reported separately by
        # the sync_link / exported_ref1c short-circuit below as
        # `already_linked`, not as skipped — semantically it means "the 1C
        # document already exists for this issue".
        if str(issue.status or "").lower() == "cancelled":
            skipped.append(
                {
                    "issue_id": int(issue.issue_id),
                    "reason": "status='cancelled', экспорт не нужен",
                }
            )
            continue

        source_ref = _clean_ref1c(issue.source_warehouse_ref1c)
        destination_ref = _clean_ref1c(issue.warehouse_ref1c)
        if source_ref and destination_ref and source_ref == destination_ref:
            skipped.append(
                {
                    "issue_id": int(issue.issue_id),
                    "reason": "source=destination: внутренний резерв, в 1С не выгружается",
                }
            )
            continue

        # Contract rule (.docs/one_c_export_from_prodplan.md): child documents
        # (here: Document_ПеремещениеЗапасов) must carry ДокументОснование
        # pointing at Document_ЗаказНаПроизводство. If the parent order has no
        # order_ref1c (not yet exported to 1C), we refuse to export the
        # transfer without a basis.
        order_ref = _clean_ref1c(issue.order.order_ref1c) if issue.order else None
        if not order_ref:
            skipped.append(
                {
                    "issue_id": int(issue.issue_id),
                    "reason": (
                        "order_ref1c пуст — родительский ЗаказНаПроизводство "
                        "ещё не выгружен в 1С, основание не сформировать"
                    ),
                }
            )
            continue

        lines: List[StockTransferExportLine] = []
        bad_line = False
        for line_number, ln in enumerate(sorted(issue.lines, key=lambda x: x.line_id), start=1):
            ref1c = _clean_ref1c(ln.component_item.item_ref1c) if ln.component_item else ""
            if not ref1c:
                skipped.append(
                    {
                        "issue_id": int(issue.issue_id),
                        "reason": f"component_item_id={ln.component_item_id}: пустой item_ref1c",
                    }
                )
                bad_line = True
                break
            lines.append(
                StockTransferExportLine(
                    line_number=line_number,
                    component_item_id=int(ln.component_item_id),
                    item_ref1c=ref1c,
                    item_name=str(ln.component_item.item_name or "") if ln.component_item else "",
                    item_article=str(ln.component_item.item_article or "")
                    if ln.component_item
                    else "",
                    unit_ref1c=_resolve_unit_ref1c(
                        db, (ln.unit or ln.component_item.unit) if ln.component_item else None
                    ),
                    qty=float(ln.required_qty or 0.0),
                )
            )
        if bad_line or not lines:
            continue

        source_ref = _clean_ref1c(issue.source_warehouse_ref1c)
        destination_ref = _clean_ref1c(issue.warehouse_ref1c)
        if not source_ref:
            skipped.append(
                {
                    "issue_id": int(issue.issue_id),
                    "reason": "склад отправитель пуст — перемещение в 1С не сформировано",
                }
            )
            continue
        if destination_ref and source_ref == destination_ref:
            skipped.append(
                {
                    "issue_id": int(issue.issue_id),
                    "reason": "склад отправитель совпадает со складом получателем — перемещение не требуется",
                }
            )
            continue

        entries.append(
            StockTransferExportEntry(
                issue_id=int(issue.issue_id),
                document_number=material_issue_number(db, issue),
                product_id=int(issue.product_id),
                order_id=int(issue.order_id),
                order_ref1c=order_ref,
                source_warehouse_ref1c=source_ref or None,
                destination_warehouse_ref1c=destination_ref or None,
                lines=lines,
                direction=str(issue.direction or "issue"),
                product_item_ref1c=(
                    _clean_ref1c(issue.product.item.item_ref1c)
                    if issue.product and issue.product.item
                    else None
                ),
                product_line_number=(
                    int(issue.product.line_number)
                    if issue.product and issue.product.line_number is not None
                    else None
                ),
                ledger_generation_id=(
                    int(issue.ledger_generation_id)
                    if issue.ledger_generation_id is not None
                    else None
                ),
                document_date=issue.created_at,
            )
        )

    return entries, skipped


def _build_header_payload(
    entry: StockTransferExportEntry,
    defaults: Optional[StockTransferExportDefaults] = None,
) -> Dict[str, Any]:
    defaults = defaults or StockTransferExportDefaults()
    comment = (
        f"PRODPLAN source=material_issue/{entry.issue_id}; "
        f"order_id={entry.order_id}; product_id={entry.product_id}; "
        f"number={entry.document_number}"
    )
    stock_lines = []
    for ln in entry.lines:
        row: Dict[str, Any] = {
            "LineNumber": ln.line_number,
            "Номенклатура_Key": ln.item_ref1c,
            "Количество": float(ln.qty),
            "КлючСвязи": int(ln.line_number),
        }
        if defaults.vat_rate_ref1c:
            row["СтавкаНДС_Key"] = defaults.vat_rate_ref1c
        _add_unit_payload(row, ln.unit_ref1c)
        stock_lines.append(row)
    document_date = _fmt_1c_datetime(entry.document_date) or _current_1c_datetime()
    payload: Dict[str, Any] = {
        "Number": entry.document_number,
        "Date": document_date,
        "Posted": False,
        "Комментарий": (
            _add_origin_marker(comment, str(entry.origin_token))
            if entry.origin_token
            else comment
        ),
        "Запасы": stock_lines,
    }
    if defaults.organization_ref1c:
        payload["Организация_Key"] = defaults.organization_ref1c
    source_unit = entry.source_warehouse_ref1c or defaults.source_structural_unit_ref1c
    destination_unit = entry.destination_warehouse_ref1c or defaults.destination_structural_unit_ref1c
    if source_unit:
        payload["СтруктурнаяЕдиница_Key"] = source_unit
    if destination_unit:
        payload["СтруктурнаяЕдиницаПолучатель_Key"] = destination_unit
    # Per contract: ДокументОснование is mandatory for child documents.
    # _collect_export_entries guarantees order_ref1c is set; this assertion
    # protects against accidental drift if the collector ever changes.
    assert entry.order_ref1c, "stock-transfer export requires order_ref1c basis"
    payload["ДокументОснование"] = entry.order_ref1c
    payload["ДокументОснование_Type"] = "StandardODATA.Document_ЗаказНаПроизводство"
    return payload


def _upsert_link(
    db: Session,
    *,
    entry: StockTransferExportEntry,
    payload_hash: str,
    target_ref_key: Optional[str],
    status: str,
    last_error: Optional[str],
) -> None:
    _upsert_sync_link(
        db,
        SyncLink,
        source_doctype="material_issue",
        source_id=int(entry.issue_id),
        target_entity=STOCK_TRANSFER_ENTITY,
        target_number=entry.document_number,
        payload_hash=payload_hash,
        target_ref_key=target_ref_key,
        status=status,
        last_error=last_error,
    )
    db.flush()
    link = _existing_link(db, int(entry.issue_id))
    if link is None:
        raise RuntimeError("material issue SyncLink upsert was not persisted")
    if link.ledger_generation_id is None and entry.ledger_generation_id is not None:
        # Creation provenance is immutable; later accepted generations do not
        # expire an operational transfer that is still tied to live MRP work.
        link.ledger_generation_id = int(entry.ledger_generation_id)


def _mark_issue_exported(
    db: Session, issue_id: int, ref_key: str
) -> None:
    issue = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .one()
    )
    issue.status = "exported"
    issue.exported_ref1c = ref_key
    issue.exported_at = datetime.now(timezone.utc)
    issue.export_error = None
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == issue.product_id)
        .first()
    )
    if state:
        state.issue_status = "exported"
    # Item-ledger  ( step 1, ): fire-and-forget enqueue of a
    # pull-by-document for the just-posted Document_ПеремещениеЗапасов. NEVER let
    # this raise into the export flow — the reconcile Balance-sweep () is the
    # safety net. No OData here: enqueue only writes a 'pending' row.
    try:
        from .item_ledger.ingest import enqueue_recorder_pull

        enqueue_recorder_pull(db, STOCK_TRANSFER_ENTITY, ref_key, source="stock_transfer_export")
    except Exception as _exc:  # noqa: BLE001
        print(f"[item-ledger] enqueue pull failed for {STOCK_TRANSFER_ENTITY} {ref_key}: {_exc}")


def _mark_issue_error(db: Session, issue_id: int, error: str) -> None:
    issue = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .one()
    )
    issue.status = "error"
    issue.export_error = error
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == issue.product_id)
        .first()
    )
    if state:
        state.issue_status = "error"


def _chain_export_parent_orders(
    db: Session,
    issue_ids: List[int],
    *,
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
    """
    Per .docs/one_c_export_from_prodplan.md: a transfer document MUST be
    created in 1C on the basis of a Document_ЗаказНаПроизводство. So before
    exporting any material_issue, ensure its parent production_order is in
    1C — auto-export the missing ones first.

    Returns the parent-export summary (or None when no parents needed export).
    """
    parent_rows = (
        db.query(ProductionOrder.order_id)
        .join(ProductionMaterialIssue, ProductionMaterialIssue.order_id == ProductionOrder.order_id)
        .join(ProductionProduct, ProductionProduct.product_id == ProductionMaterialIssue.product_id)
        .filter(ProductionMaterialIssue.issue_id.in_(list(issue_ids)))
        .filter(
            (ProductionOrder.order_ref1c.is_(None))
            | (ProductionOrder.order_ref1c == "")
            | (ProductionOrder.order_ref1c == EMPTY_REF1C)
        )
        .distinct()
        .all()
    )
    mrp_order_ids = [int(row.order_id) for row in parent_rows]
    if not mrp_order_ids:
        return None
    mrp_summary = (
        export_production_orders_to_1c(
            db,
            mrp_order_ids,
            dry_run=dry_run,
        )
        if mrp_order_ids
        else {}
    )
    return {
        **mrp_summary,
        "status": str(mrp_summary.get("status") or "ok"),
        "dry_run": bool(dry_run),
        "orders_requested": len(mrp_order_ids),
        "orders_created": int(mrp_summary.get("orders_created") or 0),
        "orders_already_linked": int(mrp_summary.get("orders_already_linked") or 0),
        "orders_error": int(mrp_summary.get("orders_error") or 0),
    }


def export_material_issues_to_1c(
    db: Session,
    issue_ids: List[int],
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Export selected ProductionMaterialIssues to 1C as Document_ПеремещениеЗапасов
    with Posted=false. Idempotent via sync_link.

    Enforces the chain rule: any parent ProductionOrder that is not yet in 1C
    is exported first (so the transfer can carry a valid ДокументОснование).
    The chain step's result is returned under summary['parent_orders_export'].
    """
    truth = require_accepted_truth(db, "production_material_issue_export")
    generation_id = int(truth.generation_id)
    requested = [int(value) for value in issue_ids if value is not None]
    issue_rows = db.query(ProductionMaterialIssue).filter(
        ProductionMaterialIssue.issue_id.in_(requested)
    ).all() if requested else []
    invalid = [
        int(issue.issue_id) for issue in issue_rows
        if issue.ledger_generation_id is None
    ]
    if invalid:
        raise ValueError(
            "material issue Ledger lineage is not current accepted truth; "
            "creation provenance is missing: "
            + ", ".join(str(value) for value in sorted(invalid))
        )
    # A material issue is an operational executor document, not a projection
    # that expires on every physical refresh.  For an older creation
    # generation, prove that its parent order still maps to current live MRP
    # work before allowing any retry or new 1C write.
    stale_orders = {
        int(issue.order_id): db.get(ProductionOrder, int(issue.order_id))
        for issue in issue_rows
        if int(issue.ledger_generation_id) != generation_id
    }
    for order_id, order in stale_orders.items():
        if order is None:
            raise ValueError(
                "material issue Ledger lineage is not current accepted truth; "
                f"parent order {order_id} is missing"
            )
        require_materialized_orders(
            db,
            [order],
            consumer="production_material_issue_export_stale_executor",
        )
    parent_export = _chain_export_parent_orders(
        db, list(issue_ids), dry_run=dry_run
    )
    entries, skipped = _collect_export_entries(db, list(issue_ids))

    eligible: List[StockTransferExportEntry] = []
    already_linked: List[StockTransferExportEntry] = []
    for entry in entries:
        issue_row = (
            db.query(ProductionMaterialIssue)
            .filter(ProductionMaterialIssue.issue_id == entry.issue_id)
            .one()
        )
        link = _existing_link(db, entry.issue_id)
        linked_ref = _clean_ref1c(link.target_ref_key) if link else ""
        exported_ref = _clean_ref1c(issue_row.exported_ref1c)
        target_ref = linked_ref or exported_ref
        entry.origin_token = _entry_origin_token(entry)

        if str(issue_row.status or "").lower() == "posted" and target_ref:
            entry.status = "existing"
            entry.target_ref_key = target_ref
            entry.reason = "перемещение уже проведено в 1С"
            already_linked.append(entry)
            continue

        if target_ref:
            entry.target_ref_key = target_ref
            entry.document_number = str(
                (link.target_number if link and link.target_number else None)
                or issue_row.document_number
            )
            entry.reason = "повторная отправка: 1С-документ уже был создан, обновляем реквизиты"
        else:
            entry.document_number = _origin_transfer_number(entry)
        eligible.append(entry)

    summary: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "entity": STOCK_TRANSFER_ENTITY,
        "issues_requested": len(issue_ids),
        "issues_eligible": len(eligible),
        "issues_already_linked": len(already_linked),
        "issues_recovered": 0,
        "issues_created": 0,
        "issues_error": 0,
        "skipped_rows": skipped,
        "entries": [],
        "parent_orders_export": parent_export,
    }

    config = _load_odata_config()
    defaults = _export_defaults(config)

    payloads: List[Dict[str, Any]] = []
    for entry in eligible:
        payload = _build_header_payload(entry, defaults)
        payloads.append(
            {"issue_id": entry.issue_id, "number": entry.document_number, "payload": payload}
        )

    if dry_run:
        summary["entries"] = [asdict(e) for e in entries]
        summary["payloads"] = payloads
        return summary

    client = _create_odata_client(config, OData1CClient)

    recovered: List[StockTransferExportEntry] = []
    pending_entries: List[StockTransferExportEntry] = []
    pending_payloads: List[Dict[str, Any]] = []
    allocated_locally = False
    for entry, envelope in zip(eligible, payloads):
        if entry.target_ref_key:
            linked_doc = _document_by_ref(client, entry.target_ref_key)
            if not _owned_live_document(entry, linked_doc):
                raise RuntimeError(
                    "Существующая ссылка перемещения 1С не подтверждена "
                    f"маркером prodplan-origin для issue {entry.issue_id}"
                )
            pending_entries.append(entry)
            pending_payloads.append(envelope)
            continue

        doc = _find_document_by_origin(
            client,
            entity=STOCK_TRANSFER_ENTITY,
            token=str(entry.origin_token),
            select_fields=["Ref_Key", "Number", "Комментарий", "Posted", "DeletionMark"],
        )
        if doc is not None and bool(doc.get("DeletionMark")):
            doc = None
        if doc is None:
            number, numbered_doc = _allocate_transfer_number(db, client, entry)
            doc = numbered_doc
            entry.document_number = number
            envelope["number"] = number
            envelope["payload"] = _build_header_payload(entry, defaults)
            issue_row = db.get(ProductionMaterialIssue, int(entry.issue_id))
            if issue_row is None:
                raise RuntimeError(f"material issue {entry.issue_id} disappeared before export")
            issue_row.document_number = number
            allocated_locally = True

        ref_key = _clean_ref1c((doc or {}).get("Ref_Key"))
        if not ref_key:
            pending_entries.append(entry)
            pending_payloads.append(envelope)
            continue

        entry.status = "existing"
        entry.target_ref_key = ref_key
        entry.document_number = str((doc or {}).get("Number") or entry.document_number)
        entry.reason = "найдено существующее перемещение 1С по prodplan-origin"
        envelope["number"] = entry.document_number
        envelope["payload"] = _build_header_payload(entry, defaults)
        _upsert_link(
            db,
            entry=entry,
            payload_hash=_payload_hash(envelope["payload"]),
            target_ref_key=ref_key,
            status="success",
            last_error=None,
        )
        issue_row = db.get(ProductionMaterialIssue, int(entry.issue_id))
        if issue_row is not None:
            issue_row.document_number = entry.document_number
        _mark_issue_exported(db, entry.issue_id, ref_key)
        recovered.append(entry)

    if recovered or allocated_locally:
        # Persist the allocated collision-free number before network I/O.  If
        # the process stops after 1C accepts the POST, the next attempt uses
        # the same origin marker and recovers instead of creating a duplicate.
        db.commit()

    for entry, payload in zip(pending_entries, pending_payloads):
        add_source_cells_to_payload(client, entry, payload["payload"])

    created, errored = _post_export_entries(
        db,
        entries=zip(pending_entries, pending_payloads),
        client=client,
        target_entity=STOCK_TRANSFER_ENTITY,
        missing_ref_error=f"1C did not return Ref_Key for new {STOCK_TRANSFER_ENTITY}",
        upsert_link=lambda **kwargs: _upsert_link(db, **kwargs),
        on_success=lambda entry, ref_key: _mark_issue_exported(db, entry.issue_id, ref_key),
        on_error=lambda entry, error: _mark_issue_error(db, entry.issue_id, error),
        log_error=lambda entry: f"[1C transfer export] issue_id={entry.issue_id} failed: {entry.error}",
    )

    summary["issues_created"] = created
    summary["issues_recovered"] = len(recovered)
    summary["issues_error"] = errored
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored == 0 else "partial_error"
    return summary
