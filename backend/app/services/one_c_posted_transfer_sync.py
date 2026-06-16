"""Pull "Posted=true" flag from 1C for previously-exported transfers.

Plan rule (Следующие этапы #4):
> Добавить sync проведённых перемещений и автоматический переход
> "К перемещению" -> "Собран".

Direction: 1C -> PRODPLAN (read-only on the 1C side; we only fetch Ref_Key
+ Posted flag). No demo-DB guard needed — we never POST/PATCH.

Source of truth: sync_link rows for material-issue exports. For each row in
status='success' with a target_ref_key set, we ask 1C "is the
Document_ПеремещениеЗапасов with this Ref_Key Posted?". If yes:
- sync_link.status -> 'posted', last_synced_at refreshed
- production_material_issues.status -> 'posted'
- ProductionOrderLineState.status -> 'assembled' (only if currently 'to_move'
  or earlier; never regresses past it)
- ProductionOrderLineState.issue_status -> 'posted'

The 1C query uses the same Posted-eq-true server filter as the existing
production_order_sync / supplier_order_sync code paths.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import (
    Item,
    ProductionMaterialIssue,
    ProductionOrderLineState,
    SyncLink,
)
from .one_c_export_common import create_odata_client as _create_odata_client
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient
from .one_c_stock_transfer_export import STOCK_TRANSFER_ENTITY


# 1C OData filters often choke on overly-long OR URLs, especially when the
# response includes the nested Запасы table part. Keep batches deliberately
# small so already-posted documents are not silently missed.
BATCH_SIZE = 15


def _fetch_pending_links(db: Session) -> List[SyncLink]:
    """sync_link rows we should re-check against 1C."""
    return (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.target_entity == STOCK_TRANSFER_ENTITY,
            SyncLink.status.in_(("success", "posted")),
            SyncLink.target_ref_key.isnot(None),
        )
        .all()
    )


def _query_posted_docs(client: OData1CClient, ref_keys: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """
    Return posted 1C transfer docs by Ref_Key, including their stock rows.

    We re-read already-posted links too: users may manually edit and re-post a
    transfer in 1C after PRODPLAN first observed Posted=true. The local
    reservation must follow the posted 1C document quantity.
    """
    found: Dict[str, Dict[str, Any]] = {}
    refs = [str(r).strip() for r in ref_keys if str(r or "").strip()]
    for i in range(0, len(refs), BATCH_SIZE):
        batch = refs[i : i + BATCH_SIZE]
        or_filter = " or ".join(f"Ref_Key eq guid'{r}'" for r in batch)
        # Server filter: Posted eq true (1C honours this; DeletionMark we
        # re-check in code as production_order_sync.py does).
        filter_q = f"({or_filter}) and Posted eq true"
        try:
            rows = client.get_all(
                STOCK_TRANSFER_ENTITY,
                filter_query=filter_q,
                select_fields=["Ref_Key", "Posted", "DeletionMark", "Запасы"],
                top=BATCH_SIZE,
                max_records=BATCH_SIZE,
                max_pages=1,
                order_by=None,
            )
        except Exception as exc:
            # Don't poison the whole batch on a network blip — keep partial
            # results.
            print(f"[posted_transfer_sync] batch query failed: {exc}")
            continue

        for rec in rows or []:
            if bool(rec.get("DeletionMark")):
                continue
            if not bool(rec.get("Posted")):
                continue
            ref = str(rec.get("Ref_Key") or "").strip()
            if ref:
                found[ref] = rec
    return found


def _posted_quantities_by_item_ref(doc: Optional[Dict[str, Any]]) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for row in (doc or {}).get("Запасы") or []:
        ref = str(row.get("Номенклатура_Key") or "").strip()
        qty = float(row.get("Количество") or 0.0)
        if ref and qty > 1e-9:
            result[ref] = result.get(ref, 0.0) + qty
    return result


def _sync_issue_lines_from_posted_doc(
    db: Session,
    issue: ProductionMaterialIssue,
    doc: Optional[Dict[str, Any]],
) -> bool:
    posted_by_ref = _posted_quantities_by_item_ref(doc)
    if not posted_by_ref:
        return False

    item_ids = [int(line.component_item_id) for line in issue.lines or [] if line.component_item_id]
    if not item_ids:
        return False
    item_ref_by_id = {
        int(item.item_id): str(item.item_ref1c or "").strip()
        for item in db.query(Item).filter(Item.item_id.in_(item_ids)).all()
    }

    changed = False
    for line in issue.lines or []:
        item_ref = item_ref_by_id.get(int(line.component_item_id))
        if not item_ref or item_ref not in posted_by_ref:
            continue
        posted_qty = posted_by_ref[item_ref]
        if abs(float(line.required_qty or 0.0) - posted_qty) > 1e-6:
            line.required_qty = posted_qty
            changed = True
        if abs(float(line.issued_qty or 0.0) - posted_qty) > 1e-6:
            line.issued_qty = posted_qty
            changed = True
    return changed


def _apply_posted(
    db: Session,
    link: SyncLink,
    doc: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Idempotent state advancement for one material-issue link whose 1C
    document is confirmed Posted. Returns (changed_anything, error).
    """
    issue = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.issue_id == int(link.source_id))
        .one_or_none()
    )
    if issue is None:
        return (False, f"material_issue id={link.source_id} не найден")

    changed = False
    now = datetime.now(timezone.utc)

    if link.status != "posted":
        link.status = "posted"
        link.last_synced_at = now
        changed = True

    if issue.status != "posted":
        issue.status = "posted"
        changed = True
    if issue.export_error:
        issue.export_error = None
        changed = True

    if _sync_issue_lines_from_posted_doc(db, issue, doc):
        changed = True

    for line in issue.lines or []:
        required = float(line.required_qty or 0.0)
        if float(line.issued_qty or 0.0) != required:
            line.issued_qty = required
            changed = True
        if str(line.line_status or "") != "issued":
            line.line_status = "issued"
            changed = True

    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == issue.product_id)
        .one_or_none()
    )
    if state is not None:
        if state.status in {"shortage", "partial", "ready", "to_move"}:
            if state.status != "assembled":
                state.status = "assembled"
                changed = True
        # else: state already past 'assembled' (produced_partial / produced /
        # cancelled) — do not regress.
        if state.issue_status != "posted":
            state.issue_status = "posted"
            changed = True

    return (changed, None)


def sync_posted_transfers(db: Session, *, dry_run: bool = False) -> Dict[str, Any]:
    """
    Read-only against 1C. Picks all material-issue exports that landed
    successfully (sync_link.status='success'), asks 1C which of them are
    Posted=true now, and locally promotes them to status='posted' +
    line-state='assembled'.

    `dry_run=True` performs the same 1C reads but skips DB writes — useful
    for diagnostics.
    """
    pending = _fetch_pending_links(db)
    by_ref: Dict[str, SyncLink] = {}
    for link in pending:
        ref = str(link.target_ref_key or "").strip()
        if ref:
            by_ref[ref] = link

    summary: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "entity": STOCK_TRANSFER_ENTITY,
        "candidates": len(by_ref),
        "posted_found": 0,
        "advanced": 0,
        "errors": [],
        "details": [],
    }

    if not by_ref:
        return summary

    client = _create_odata_client(_load_odata_config(), OData1CClient)

    posted_docs = _query_posted_docs(client, by_ref.keys())
    summary["posted_found"] = len(posted_docs)

    advanced = 0
    errors: List[str] = []
    for ref, doc in posted_docs.items():
        link = by_ref.get(ref)
        if link is None:
            continue
        try:
            changed, err = _apply_posted(db, link, doc)
            if err:
                errors.append(err)
                continue
            if changed:
                advanced += 1
                summary["details"].append(
                    {
                        "issue_id": int(link.source_id),
                        "target_ref_key": ref,
                        "target_number": link.target_number,
                    }
                )
        except Exception as exc:
            errors.append(f"issue_id={link.source_id}: {exc}")

    if dry_run:
        db.rollback()
    else:
        db.commit()

    summary["advanced"] = advanced
    summary["errors"] = errors
    if errors:
        summary["status"] = "partial_error"
    return summary
