"""Export ProductionMaterialIssue documents to 1C as Document_ПеремещениеЗапасов.

Pattern: mirrors one_c_production_order_export.py.
Documentation: .docs/one_c_export_from_prodplan.md.

Safety per the doc:
1. Default dry_run=True.
2. Refuse non-demo base_url unless allow_production=True.
3. Posted=false (proceedng stays on 1C admin side).
4. Idempotency via sync_link (source_doctype='material_issue').

A material issue in PRODPLAN models the warehouse-to-workshop transfer of
the components needed for one production_products line. In 1C this is a
Document_ПеремещениеЗапасов with СкладОтправитель_Key (source) and
СкладПолучатель_Key (destination), plus a Запасы table part with the
components.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    Item,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SyncLink,
)
from .one_c_export_common import (
    clean_ref1c as _clean_ref1c,
    create_odata_client as _create_odata_client,
    fmt_1c_datetime as _fmt_1c_datetime,
    find_sync_link as _find_sync_link,
    post_export_entries as _post_export_entries,
    upsert_sync_link as _upsert_sync_link,
)
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient


STOCK_TRANSFER_ENTITY = "Document_ПеремещениеЗапасов"
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"


@dataclass
class StockTransferExportLine:
    line_number: int
    component_item_id: int
    item_ref1c: str
    item_name: str
    item_article: str
    qty: float


@dataclass
class StockTransferExportEntry:
    issue_id: int
    document_number: str
    product_id: int
    order_id: int
    source_warehouse_ref1c: Optional[str]
    destination_warehouse_ref1c: Optional[str]
    lines: List[StockTransferExportLine] = field(default_factory=list)
    target_ref_key: Optional[str] = None
    status: str = "planned"
    error: Optional[str] = None
    reason: Optional[str] = None


def _existing_link(db: Session, issue_id: int) -> Optional[SyncLink]:
    return _find_sync_link(
        db,
        SyncLink,
        source_doctype="material_issue",
        source_id=int(issue_id),
        target_entity=STOCK_TRANSFER_ENTITY,
    )


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

        lines: List[StockTransferExportLine] = []
        bad_line = False
        for ln in sorted(issue.lines, key=lambda x: x.line_id):
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
                    line_number=int(ln.line_id),
                    component_item_id=int(ln.component_item_id),
                    item_ref1c=ref1c,
                    item_name=str(ln.component_item.item_name or "") if ln.component_item else "",
                    item_article=str(ln.component_item.item_article or "")
                    if ln.component_item
                    else "",
                    qty=float(ln.required_qty or 0.0),
                )
            )
        if bad_line or not lines:
            continue

        entries.append(
            StockTransferExportEntry(
                issue_id=int(issue.issue_id),
                document_number=str(issue.document_number),
                product_id=int(issue.product_id),
                order_id=int(issue.order_id),
                source_warehouse_ref1c=_clean_ref1c(issue.source_warehouse_ref1c) or None,
                destination_warehouse_ref1c=_clean_ref1c(issue.warehouse_ref1c) or None,
                lines=lines,
            )
        )

    return entries, skipped


def _build_header_payload(entry: StockTransferExportEntry) -> Dict[str, Any]:
    comment = (
        f"PRODPLAN source=material_issue/{entry.issue_id}; "
        f"order_id={entry.order_id}; product_id={entry.product_id}; "
        f"number={entry.document_number}"
    )
    stock_lines = [
        {
            "LineNumber": ln.line_number,
            "Номенклатура_Key": ln.item_ref1c,
            "Количество": float(ln.qty),
        }
        for ln in entry.lines
    ]
    payload: Dict[str, Any] = {
        "Number": entry.document_number,
        "Date": _fmt_1c_datetime(date.today()),
        "Posted": False,
        "Комментарий": comment,
        "Запасы": stock_lines,
    }
    if entry.source_warehouse_ref1c:
        payload["СкладОтправитель_Key"] = entry.source_warehouse_ref1c
    if entry.destination_warehouse_ref1c:
        payload["СкладПолучатель_Key"] = entry.destination_warehouse_ref1c
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
    issue.exported_at = datetime.utcnow()
    issue.export_error = None
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == issue.product_id)
        .first()
    )
    if state:
        state.issue_status = "exported"


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


def export_material_issues_to_1c(
    db: Session,
    issue_ids: List[int],
    *,
    dry_run: bool = True,
    allow_production: bool = False,
) -> Dict[str, Any]:
    """
    Export selected ProductionMaterialIssues to 1C as Document_ПеремещениеЗапасов
    with Posted=false. Idempotent via sync_link.
    """
    entries, skipped = _collect_export_entries(db, list(issue_ids))

    eligible: List[StockTransferExportEntry] = []
    already_linked: List[StockTransferExportEntry] = []
    for entry in entries:
        link = _existing_link(db, entry.issue_id)
        if link and link.status == "success" and (link.target_ref_key or ""):
            entry.status = "existing"
            entry.target_ref_key = str(link.target_ref_key)
            entry.reason = "уже выгружен в 1С (sync_link)"
            already_linked.append(entry)
            continue
        issue_row = (
            db.query(ProductionMaterialIssue)
            .filter(ProductionMaterialIssue.issue_id == entry.issue_id)
            .one()
        )
        if _clean_ref1c(issue_row.exported_ref1c):
            entry.status = "existing"
            entry.target_ref_key = _clean_ref1c(issue_row.exported_ref1c)
            entry.reason = "exported_ref1c уже стоит"
            already_linked.append(entry)
            continue
        eligible.append(entry)

    summary: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "entity": STOCK_TRANSFER_ENTITY,
        "issues_requested": len(issue_ids),
        "issues_eligible": len(eligible),
        "issues_already_linked": len(already_linked),
        "issues_created": 0,
        "issues_error": 0,
        "skipped_rows": skipped,
        "entries": [],
    }

    payloads: List[Dict[str, Any]] = []
    for entry in eligible:
        payload = _build_header_payload(entry)
        payloads.append(
            {"issue_id": entry.issue_id, "number": entry.document_number, "payload": payload}
        )

    if dry_run:
        summary["entries"] = [asdict(e) for e in entries]
        summary["payloads"] = payloads
        return summary

    client = _create_odata_client(
        _load_odata_config(),
        OData1CClient,
        allow_production=allow_production,
        require_demo_base=True,
    )

    created, errored = _post_export_entries(
        db,
        entries=zip(eligible, payloads),
        client=client,
        target_entity=STOCK_TRANSFER_ENTITY,
        missing_ref_error=f"1C did not return Ref_Key for new {STOCK_TRANSFER_ENTITY}",
        upsert_link=lambda **kwargs: _upsert_link(db, **kwargs),
        on_success=lambda entry, ref_key: _mark_issue_exported(db, entry.issue_id, ref_key),
        on_error=lambda entry, error: _mark_issue_error(db, entry.issue_id, error),
        log_error=lambda entry: f"[1C transfer export] issue_id={entry.issue_id} failed: {entry.error}",
    )

    summary["issues_created"] = created
    summary["issues_error"] = errored
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored == 0 else "partial_error"
    return summary
