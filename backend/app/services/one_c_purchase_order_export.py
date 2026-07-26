from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import (
    Item,
    PlannedPurchase,
    PurchaseExportLineAllocation,
    SyncLink,
    Unit,
)
from .mrp_mutation_guard import require_current_run, require_selected_proposals
from .one_c_export_common import (
    add_origin_marker as _add_origin_marker,
    clean_ref1c as _clean_ref1c,
    create_odata_client as _create_odata_client,
    fmt_1c_datetime as _fmt_1c_datetime,
    find_document_by_origin as _find_document_by_origin,
    upsert_sync_link as _upsert_sync_link,
)
from .one_c_document_numbers import purchase_order_number
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient

PURCHASE_ORDER_ENTITY = "Document_ЗаказПоставщику"


def create_purchase_order_document(
    client: OData1CClient,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """The single transport-level create point for 1C purchase orders."""
    result = client.post(PURCHASE_ORDER_ENTITY, payload)
    return result if isinstance(result, dict) else {}
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"
UNIT_TYPE_1C = "StandardODATA.Catalog_КлассификаторЕдиницИзмерения"


@dataclass
class PurchaseOrderExportLine:
    purchase_ids: List[int]
    item_id: int
    item_ref1c: str
    item_name: str
    item_article: str
    unit_ref1c: str
    unit_name: str
    qty: float
    need_date: Optional[str]
    order_date: Optional[str]
    purchase_qty_by_id: Dict[int, float] = field(default_factory=dict)
    request_line_token: Optional[int] = None
    export_line_payload_hash: Optional[str] = None


@dataclass
class PurchaseOrderExportGroup:
    supplier_ref1c: str
    number: str
    lines: List[PurchaseOrderExportLine] = field(default_factory=list)
    target_ref_key: Optional[str] = None
    status: str = "planned"
    error: Optional[str] = None


def _short_order_number(run_id: int, index: int) -> str:
    return purchase_order_number(run_id, index)


def _existing_order_by_number(client: OData1CClient, number: str) -> Optional[Dict[str, Any]]:
    rows = client.get_all(
        PURCHASE_ORDER_ENTITY,
        filter_query=f"Number eq '{number}'",
        select_fields=["Ref_Key", "Number", "Контрагент_Key", "Комментарий", "Запасы"],
        top=1,
        max_records=1,
        max_pages=1,
        order_by=None,
    )
    return rows[0] if rows else None


def _collect_purchase_groups(
    db: Session,
    run_id: int,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    purchase_ids: Optional[List[int]] = None,
    exclude_exported: bool = True,
) -> Tuple[List[PurchaseOrderExportGroup], List[Dict[str, Any]], List[int]]:
    q = (
        db.query(
            PlannedPurchase.purchase_id,
            PlannedPurchase.item_id,
            PlannedPurchase.qty,
            PlannedPurchase.need_date,
            PlannedPurchase.order_date,
            PlannedPurchase.supplier_ref1c,
            Item.item_ref1c,
            Item.supplier_ref1c.label("item_supplier_ref1c"),
            Item.item_name,
            Item.item_article,
            Item.unit,
            Unit.short_name,
            Unit.unit_name,
            Unit.unit_code,
        )
        .join(Item, PlannedPurchase.item_id == Item.item_id)
        .outerjoin(Unit, Item.unit == Unit.unit_ref1c)
        .filter(PlannedPurchase.run_id == int(run_id))
    )
    if date_from:
        q = q.filter(PlannedPurchase.bucket_date >= date.fromisoformat(date_from))
    if date_to:
        q = q.filter(PlannedPurchase.bucket_date <= date.fromisoformat(date_to))
    selected_ids = sorted({int(pid) for pid in (purchase_ids or []) if int(pid) > 0})
    if selected_ids:
        q = q.filter(PlannedPurchase.purchase_id.in_(selected_ids))
    q = q.order_by(PlannedPurchase.purchase_id.asc())

    # Links are scoped to the exact candidate set.  A SyncLink is an external
    # side effect, not a global suppression list: a link from another run or
    # outside this requested window must not hide a proposal from this export.
    candidate_ids = [int(row.purchase_id) for row in q.all()]
    already_exported: set[int] = {
        int(pid)
        for (pid,) in db.query(SyncLink.source_id)
        .join(PlannedPurchase, PlannedPurchase.purchase_id == SyncLink.source_id)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "planned_purchase",
            SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
            SyncLink.status == "success",
            PlannedPurchase.run_id == int(run_id),
            SyncLink.source_id.in_(candidate_ids or [-1]),
        )
        .all()
    } if exclude_exported else set()

    missing: List[Dict[str, Any]] = []
    grouped: Dict[str, Dict[Tuple[int, str, Optional[str]], PurchaseOrderExportLine]] = {}

    for row in q.all():
        if int(row.purchase_id) in already_exported:
            continue
        supplier_ref = (row.supplier_ref1c or row.item_supplier_ref1c or "").strip()
        supplier_ref = _clean_ref1c(supplier_ref)
        item_ref = _clean_ref1c(row.item_ref1c)
        qty = float(row.qty or 0.0)
        if qty <= 0:
            continue
        if not supplier_ref or not item_ref:
            missing.append(
                {
                    "purchase_id": int(row.purchase_id),
                    "item_id": int(row.item_id),
                    "item_name": row.item_name,
                    "missing_supplier": not bool(supplier_ref),
                    "missing_item_ref1c": not bool(item_ref),
                }
            )
            continue

        unit_ref1c = _clean_ref1c(row.unit)
        unit_name = (row.short_name or row.unit_name or row.unit_code or row.unit or "").strip()
        need_iso = row.need_date.isoformat() if row.need_date else None
        order_iso = row.order_date.isoformat() if row.order_date else None
        key = (int(row.item_id), unit_ref1c or unit_name, need_iso)
        supplier_bucket = grouped.setdefault(supplier_ref, {})
        if key not in supplier_bucket:
            supplier_bucket[key] = PurchaseOrderExportLine(
                purchase_ids=[],
                item_id=int(row.item_id),
                item_ref1c=item_ref,
                item_name=row.item_name or "",
                item_article=row.item_article or "",
                unit_ref1c=unit_ref1c,
                unit_name=unit_name,
                qty=0.0,
                need_date=need_iso,
                order_date=order_iso,
            )
        supplier_bucket[key].purchase_ids.append(int(row.purchase_id))
        supplier_bucket[key].qty += qty
        supplier_bucket[key].purchase_qty_by_id[int(row.purchase_id)] = qty

    groups: List[PurchaseOrderExportGroup] = []
    for idx, supplier_ref in enumerate(sorted(grouped.keys()), start=1):
        lines = sorted(
            grouped[supplier_ref].values(),
            key=lambda line: ((line.need_date or ""), line.item_name.lower(), line.item_article.lower()),
        )
        groups.append(
            PurchaseOrderExportGroup(
                supplier_ref1c=supplier_ref,
                number=_short_order_number(run_id, idx),
                lines=lines,
            )
        )
    return groups, missing, sorted(already_exported)


def _verify_linked_retry_groups(
    db: Session,
    *,
    run_id: int,
    generation_id: int,
    freeze_version: int,
    exported_purchase_ids: List[int],
) -> None:
    """Fail closed before any OData call when a successful group is retried.

    The current selected proposal may be only one member of a coalesced order,
    so rebuild the *whole* saved target group from its SyncLinks.  Quantity is
    not enough: supplier/item/unit/date and the immutable line token/hash must
    all still describe the same obligation.
    """
    if not exported_purchase_ids:
        return
    seed_links = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "planned_purchase",
            SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
            SyncLink.status == "success",
            SyncLink.source_id.in_(exported_purchase_ids),
        )
        .all()
    )
    by_target: Dict[str, List[SyncLink]] = {}
    for link in seed_links:
        target = _clean_ref1c(link.target_ref_key)
        if not target or link.ledger_generation_id is None or not link.payload_hash:
            raise RuntimeError("purchase export retry has legacy or incomplete SyncLink")
        if int(link.ledger_generation_id) != int(generation_id):
            raise RuntimeError("purchase export retry belongs to another Ledger generation")
        by_target.setdefault(target, []).append(link)

    for target_ref, seeds in by_target.items():
        group_links = (
            db.query(SyncLink)
            .join(PlannedPurchase, PlannedPurchase.purchase_id == SyncLink.source_id)
            .filter(
                SyncLink.source_system == "PRODPLAN",
                SyncLink.source_doctype == "planned_purchase",
                SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
                SyncLink.status == "success",
                SyncLink.target_ref_key == target_ref,
                PlannedPurchase.run_id == int(run_id),
            )
            .all()
        )
        if not group_links:
            raise RuntimeError("purchase export retry target has no current-run SyncLinks")
        proposal_ids = sorted({int(link.source_id) for link in group_links})
        groups, missing, _ = _collect_purchase_groups(
            db, run_id, purchase_ids=proposal_ids, exclude_exported=False,
        )
        if missing or len(groups) != 1:
            raise RuntimeError("purchase export retry cannot reconstruct one exact supplier group")
        group = groups[0]
        _stamp_line_tokens(
            group, run_id=run_id, generation_id=generation_id, freeze_version=freeze_version,
        )
        expected_group_hash = _group_export_payload_hash(
            group, run_id=run_id, generation_id=generation_id, freeze_version=freeze_version,
        )
        expected_by_purchase: Dict[int, PurchaseOrderExportLine] = {
            int(purchase_id): line
            for line in group.lines
            for purchase_id in line.purchase_ids
        }
        if set(expected_by_purchase) != set(proposal_ids):
            raise RuntimeError("purchase export retry has ambiguous proposal membership")
        for link in group_links:
            if (
                link.ledger_generation_id is None
                or int(link.ledger_generation_id) != int(generation_id)
                or link.payload_hash != expected_group_hash
                or _clean_ref1c(link.target_ref_key) != target_ref
            ):
                raise RuntimeError("purchase export retry group payload changed")
            expected = expected_by_purchase[int(link.source_id)]
            allocations = (
                db.query(PurchaseExportLineAllocation)
                .filter_by(
                    ledger_generation_id=int(generation_id),
                    planned_purchase_id=int(link.source_id),
                )
                .all()
            )
            if len(allocations) != 1:
                raise RuntimeError("purchase export retry has ambiguous exact line allocation")
            allocation = allocations[0]
            expected_qty = expected.purchase_qty_by_id[int(link.source_id)]
            if (
                _clean_ref1c(allocation.supplier_order_ref) != target_ref
                or abs(float(allocation.allocated_qty) - float(expected_qty)) > 1e-6
                or allocation.request_line_token != expected.request_line_token
                or allocation.export_line_payload_hash != expected.export_line_payload_hash
            ):
                raise RuntimeError("purchase export retry allocation axes changed")


def _verify_pending_candidate_links(
    db: Session,
    *,
    run_id: int,
    generation_id: int,
    freeze_version: int,
    groups: List[PurchaseOrderExportGroup],
) -> None:
    """Reject stale planned/error links before they can reach 1C again."""
    group_by_purchase = {
        int(purchase_id): group
        for group in groups
        for line in group.lines
        for purchase_id in line.purchase_ids
    }
    if not group_by_purchase:
        return
    links = (
        db.query(SyncLink)
        .join(PlannedPurchase, PlannedPurchase.purchase_id == SyncLink.source_id)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "planned_purchase",
            SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
            PlannedPurchase.run_id == int(run_id),
            SyncLink.source_id.in_(list(group_by_purchase)),
        )
        .all()
    )
    for link in links:
        # Successful links were removed from ``groups`` and are checked by the
        # exact-allocation retry verifier.  Every other persisted state could
        # be a post-success/local-failure window and must be equally immutable.
        if link.status == "success":
            continue
        group = group_by_purchase[int(link.source_id)]
        expected_hash = _group_export_payload_hash(
            group, run_id=run_id, generation_id=generation_id, freeze_version=freeze_version,
        )
        if (
            link.ledger_generation_id is None
            or int(link.ledger_generation_id) != int(generation_id)
            or not link.payload_hash
            or link.payload_hash != expected_hash
        ):
            raise RuntimeError("purchase export pending SyncLink payload changed or is legacy")


def _canonical_value(value: Any) -> Any:
    """Canonical JSON primitive used for durable 1C line identity."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f") if value else "0"
    if isinstance(value, dict):
        return {str(k): _canonical_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(v) for v in value]
    return value


def _canonical_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(_canonical_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _positive_63bit_token(payload: Dict[str, Any]) -> int:
    value = int.from_bytes(hashlib.sha256(
        json.dumps(_canonical_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()[:8], "big") & ((1 << 63) - 1)
    return value or 1


def _canonical_group_payload(
    group: PurchaseOrderExportGroup, *, run_id: int, generation_id: int, freeze_version: int,
) -> Dict[str, Any]:
    """The immutable business identity of one supplier-order delta.

    Deliberately excludes document number/ref, SyncLink status and errors: all
    of those change during retries and must never alter the obligation hash.
    """
    lines = [
        {
            "item": line.item_ref1c,
            "characteristic": EMPTY_REF1C,
            "unit": line.unit_ref1c or line.unit_name,
            "qty": line.qty,
            "need_date": line.need_date,
            "order_date": line.order_date,
        }
        for line in group.lines
    ]
    lines.sort(key=lambda line: (
        str(line["item"] or ""), str(line["unit"] or ""),
        str(line["need_date"] or ""), str(line["order_date"] or ""), str(line["qty"]),
    ))
    return {
        "v": 2,
        "kind": "purchase_export_group",
        "run": int(run_id),
        "generation": int(generation_id),
        "freeze": int(freeze_version),
        "supplier": group.supplier_ref1c,
        "lines": lines,
    }


def _group_export_payload_hash(
    group: PurchaseOrderExportGroup, *, run_id: int, generation_id: int, freeze_version: int,
) -> str:
    return _canonical_hash(_canonical_group_payload(
        group, run_id=run_id, generation_id=generation_id, freeze_version=freeze_version,
    ))


def _stamp_line_tokens(
    group: PurchaseOrderExportGroup, *, run_id: int, generation_id: int, freeze_version: int,
) -> None:
    """Assign a versioned, deterministic positive Int64 to every outgoing 1C line.

    A collision is a hard pre-network error: otherwise a supplier receipt can
    never be reconciled to one exact planning obligation.
    """
    batch = _group_export_payload_hash(
        group, run_id=run_id, generation_id=generation_id, freeze_version=freeze_version,
    )
    seen: set[int] = set()
    for line in group.lines:
        axes = {
            "v": 2, "kind": "purchase_export_line", "group": batch,
            "generation": generation_id, "run": run_id, "freeze": freeze_version,
            "supplier": group.supplier_ref1c, "item": line.item_ref1c,
            "characteristic": EMPTY_REF1C, "unit": line.unit_ref1c or line.unit_name,
            "qty": line.qty, "need_date": line.need_date, "order_date": line.order_date,
        }
        token = _positive_63bit_token(axes)
        if token in seen:
            raise RuntimeError("1C КлючСвязи collision inside purchase export group")
        seen.add(token)
        line.request_line_token = token
        line.export_line_payload_hash = _canonical_hash(axes)


def _order_lines_payload(ref_key: str, group: PurchaseOrderExportGroup) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line_no, line in enumerate(group.lines, start=1):
        row = {
            "LineNumber": line_no,
            "Номенклатура_Key": line.item_ref1c,
            "Характеристика_Key": EMPTY_REF1C,
            "Количество": float(line.qty or 0.0),
            "ДатаПоступления": _fmt_1c_datetime(date.fromisoformat(line.need_date)) if line.need_date else None,
            "Содержание": line.item_name,
            "Цена": 0,
            "ПроцентСкидкиНаценки": 0,
            "СуммаСкидкиНаценки": 0,
            "Сумма": 0,
            "СтавкаНДС_Key": EMPTY_REF1C,
            "СуммаНДС": 0,
            "Всего": 0,
            "Спецификация_Key": EMPTY_REF1C,
            "ЗаказПокупателя_Key": EMPTY_REF1C,
            "СтруктурнаяЕдиницаРезерв_Key": EMPTY_REF1C,
            "НоменклатураПоставщика_Key": EMPTY_REF1C,
            "КлючСвязи": line.request_line_token,
        }
        if line.unit_ref1c:
            row["ЕдиницаИзмерения"] = line.unit_ref1c
            row["ЕдиницаИзмерения_Type"] = UNIT_TYPE_1C
        elif line.unit_name:
            row["ЕдиницаИзмерения"] = line.unit_name
        if ref_key:
            row["Ref_Key"] = ref_key
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows


def _doc_endpoint(ref_key: str) -> str:
    return f"{PURCHASE_ORDER_ENTITY}(guid'{ref_key}')"


def _purchase_ids_for_group(group: PurchaseOrderExportGroup) -> List[int]:
    return sorted({int(pid) for line in group.lines for pid in line.purchase_ids})


def _upsert_purchase_links(
    db: Session,
    group: PurchaseOrderExportGroup,
    *,
    payload_hash: str,
    target_ref_key: Optional[str],
    status: str,
    last_error: Optional[str],
    generation_id: int,
) -> None:
    for purchase_id in _purchase_ids_for_group(group):
        _upsert_sync_link(
            db,
            SyncLink,
            source_doctype="planned_purchase",
            source_id=int(purchase_id),
            target_entity=PURCHASE_ORDER_ENTITY,
            target_number=group.number,
            payload_hash=payload_hash,
            target_ref_key=target_ref_key,
            status=status,
            last_error=last_error,
        )
        db.flush()
        link = (
            db.query(SyncLink)
            .filter_by(source_system="PRODPLAN", source_doctype="planned_purchase",
                       source_id=int(purchase_id), target_entity=PURCHASE_ORDER_ENTITY)
            .one()
        )
        if link.ledger_generation_id is not None and int(link.ledger_generation_id) != int(generation_id):
            raise RuntimeError("purchase SyncLink belongs to another Ledger generation")
        link.ledger_generation_id = int(generation_id)


def _record_exact_line_allocations(
    db: Session,
    *,
    group: PurchaseOrderExportGroup,
    document: Dict[str, Any],
    generation_id: int,
) -> None:
    """Persist the exact 1C line identity; never infer it on a later sync."""
    if _clean_ref1c(document.get("Контрагент_Key")) != group.supplier_ref1c:
        raise RuntimeError("1C purchase export returned a supplier header mismatch")
    returned = document.get("Запасы")
    if not isinstance(returned, list) or len(returned) != len(group.lines):
        raise RuntimeError(
            "1C purchase export did not return exact order lines; "
            "cannot persist PurchaseExportLineAllocation"
        )
    supplier_order_ref = _clean_ref1c(document.get("Ref_Key")) or group.target_ref_key
    if not supplier_order_ref:
        raise RuntimeError("1C purchase export did not return exact order Ref_Key")
    expected_by_token = {line.request_line_token: line for line in group.lines}
    if None in expected_by_token or len(expected_by_token) != len(group.lines):
        raise RuntimeError("purchase export line token was not built uniquely")
    exact_rows: List[Tuple[PurchaseOrderExportLine, str]] = []
    for actual in returned:
        token = actual.get("КлючСвязи")
        try:
            expected = expected_by_token.pop(int(token))
        except (TypeError, ValueError, KeyError):
            raise RuntimeError("1C purchase export returned unknown or duplicate КлючСвязи")
        line_no = actual.get("LineNumber")
        if line_no is None:
            raise RuntimeError("1C purchase export line has no exact LineNumber")
        actual_unit = _clean_ref1c(actual.get("ЕдиницаИзмерения"))
        expected_unit = expected.unit_ref1c or expected.unit_name
        raw_characteristic = str(actual.get("Характеристика_Key") or "").strip()
        raw_need_date = str(actual.get("ДатаПоступления") or "").strip()
        try:
            actual_need_date = datetime.fromisoformat(
                raw_need_date.replace("Z", "+00:00")
            ).date().isoformat()
        except ValueError:
            actual_need_date = ""
        if (
            _clean_ref1c(actual.get("Номенклатура_Key")) != expected.item_ref1c
            or raw_characteristic != EMPTY_REF1C
            or abs(float(actual.get("Количество") or 0) - float(expected.qty)) > 1e-6
            or actual_unit != expected_unit
            or actual_need_date != (expected.need_date or "")
        ):
            raise RuntimeError("1C purchase export returned a line payload mismatch")
        exact_rows.append((expected, str(line_no)))
    if expected_by_token:
        raise RuntimeError("1C purchase export did not return every request line token")

    for expected, line_no in exact_rows:
        for purchase_id, qty in expected.purchase_qty_by_id.items():
            existing = (
                db.query(PurchaseExportLineAllocation)
                .filter_by(
                    ledger_generation_id=generation_id,
                    supplier_order_ref=supplier_order_ref,
                    supplier_order_line_no=line_no,
                    planned_purchase_id=int(purchase_id),
                )
                .one_or_none()
            )
            if existing is not None:
                if (
                    abs(float(existing.allocated_qty) - float(qty)) > 1e-6
                    or existing.request_line_token != int(expected.request_line_token)
                    or existing.export_line_payload_hash != expected.export_line_payload_hash
                ):
                    raise RuntimeError("purchase export allocation retry payload changed")
                continue
            db.add(
                PurchaseExportLineAllocation(
                    ledger_generation_id=generation_id,
                    supplier_order_ref=supplier_order_ref,
                    supplier_order_line_no=line_no,
                    planned_purchase_id=int(purchase_id),
                    allocated_qty=float(qty),
                    request_line_token=int(expected.request_line_token),
                    export_line_payload_hash=expected.export_line_payload_hash,
                )
            )


def _has_stock_lines(doc: Dict[str, Any]) -> bool:
    lines = doc.get("Запасы")
    return isinstance(lines, list) and len(lines) > 0


def _group_batch_token(group: PurchaseOrderExportGroup) -> str:
    """Generation-bound 1C recovery key built from stamped line obligations."""
    tokens = [line.request_line_token for line in group.lines]
    if not tokens or any(token is None for token in tokens) or len(set(tokens)) != len(tokens):
        raise RuntimeError("purchase origin batch requires uniquely stamped export lines")
    return _canonical_hash({
        "v": 3,
        "kind": "purchase_origin_batch",
        "supplier": group.supplier_ref1c,
        "request_line_tokens": sorted(int(token) for token in tokens),
    })


def _order_comment(run_id: int, group: PurchaseOrderExportGroup) -> str:
    return _add_origin_marker(
        f"PRODPLAN source=planned_purchase/run:{int(run_id)}; "
        f"number={group.number}; batch={_group_batch_token(group)}",
        _group_batch_token(group)[:32],
    )


def _has_batch_token(doc: Dict[str, Any], group: PurchaseOrderExportGroup) -> bool:
    comment = str(doc.get("Комментарий") or "")
    return f"batch={_group_batch_token(group)}" in comment


def _is_prodplan_order_for_run(doc: Dict[str, Any], run_id: int) -> bool:
    comment = str(doc.get("Комментарий") or "")
    return f"PRODPLAN source=planned_purchase/run:{int(run_id)}" in comment


def _ensure_free_or_reusable_number(
    client: OData1CClient,
    group: PurchaseOrderExportGroup,
    run_id: int,
    start_index: int,
) -> Optional[Dict[str, Any]]:
    # Number is human-facing and may be reallocated by another planning run.
    # The comment marker is the cross-instance recovery key.
    by_origin = _find_document_by_origin(
        client,
        entity=PURCHASE_ORDER_ENTITY,
        token=_group_batch_token(group)[:32],
        select_fields=["Ref_Key", "Number", "Контрагент_Key", "Комментарий", "Запасы"],
    )
    if by_origin:
        group.number = str(by_origin.get("Number") or group.number)
        return by_origin
    index = start_index
    while index < start_index + 1000:
        group.number = _short_order_number(run_id, index)
        existing = _existing_order_by_number(client, group.number)
        if not existing:
            return None
        existing_supplier = _clean_ref1c(existing.get("Контрагент_Key"))
        if (
            existing_supplier == group.supplier_ref1c
            and _is_prodplan_order_for_run(existing, run_id)
        ):
            # An empty header is safe to reuse.  A filled document is reusable
            # only when it is the exact same delta: this is the recovery path
            # for "POST succeeded, local SyncLink commit failed".  A filled
            # older batch must never be treated as if it contained new qty.
            if not _has_stock_lines(existing) or _has_batch_token(existing, group):
                return existing
        index += 1
    raise RuntimeError(f"Не удалось подобрать свободный номер заказа для поставщика {group.supplier_ref1c}")


def export_planned_purchases_to_1c(
    db: Session,
    run_id: int,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    purchase_ids: Optional[List[int]] = None,
    dry_run: bool = False,
    allow_production: bool = False,
) -> Dict[str, Any]:
    run, generation_id = require_current_run(
        db, int(run_id), consumer="one_c_purchase_order_export"
    )
    proposal_query = db.query(PlannedPurchase).filter(
        PlannedPurchase.run_id == int(run_id)
    )
    selected_ids = sorted({int(pid) for pid in (purchase_ids or [])})
    if selected_ids:
        proposal_query = proposal_query.filter(
            PlannedPurchase.purchase_id.in_(selected_ids)
        )
    proposals = proposal_query.all()
    if selected_ids and {int(row.purchase_id) for row in proposals} != set(selected_ids):
        raise ValueError("one or more selected planned purchases do not exist in the run")
    require_selected_proposals(
        db,
        proposals,
        run=run,
        generation_id=generation_id,
        consumer="one_c_purchase_order_export",
    )
    groups, skipped_rows, already_exported_ids = _collect_purchase_groups(
        db,
        run_id,
        date_from=date_from,
        date_to=date_to,
        purchase_ids=purchase_ids,
    )
    for group in groups:
        _stamp_line_tokens(
            group, run_id=int(run.run_id), generation_id=int(generation_id),
            freeze_version=int(run.active_freeze_version),
        )
    _verify_pending_candidate_links(
        db,
        run_id=int(run.run_id),
        generation_id=int(generation_id),
        freeze_version=int(run.active_freeze_version),
        groups=groups,
    )
    _verify_linked_retry_groups(
        db,
        run_id=int(run.run_id),
        generation_id=int(generation_id),
        freeze_version=int(run.active_freeze_version),
        exported_purchase_ids=already_exported_ids,
    )
    if dry_run:
        return {
            "status": "ok",
            "dry_run": True,
            "orders_planned": len(groups),
            "orders_created": 0,
            "orders_existing": 0,
            "lines_total": sum(len(g.lines) for g in groups),
            "skipped_rows": skipped_rows,
            "already_exported_purchase_ids": already_exported_ids,
            "orders": [asdict(g) for g in groups],
        }

    # All selected proposals already have an exact immutable allocation.  A
    # no-op retry must not even construct an OData client (important over the
    # remote tunnel, and proves this path cannot mutate 1C).
    if not groups:
        return {
            "status": "ok",
            "dry_run": False,
            "orders_planned": 0,
            "orders_created": 0,
            "orders_existing": 0,
            "lines_total": 0,
            "skipped_rows": skipped_rows,
            "already_exported_purchase_ids": already_exported_ids,
            "orders": [],
        }

    client = _create_odata_client(
        _load_odata_config(),
        OData1CClient,
        allow_production=allow_production,
        require_demo_base=True,
    )

    created = 0
    existing = 0
    for group_index, group in enumerate(groups, start=1):
        try:
            existing_doc = _ensure_free_or_reusable_number(client, group, run_id, group_index)
            if existing_doc:
                group.target_ref_key = str(existing_doc.get("Ref_Key") or "") or None
                if not _has_stock_lines(existing_doc):
                    if not group.target_ref_key:
                        raise RuntimeError(f"1C did not return Ref_Key for existing {group.number}")
                    patch_payload = {
                        "Комментарий": _order_comment(run_id, group),
                        "Запасы": _order_lines_payload(group.target_ref_key, group),
                    }
                    client.patch(_doc_endpoint(group.target_ref_key), patch_payload)
                    group.status = "created"
                    created += 1
                else:
                    group.status = "existing"
                    existing += 1
                exact_doc = _find_document_by_origin(
                    client,
                    entity=PURCHASE_ORDER_ENTITY,
                    token=_group_batch_token(group)[:32],
                    select_fields=["Ref_Key", "Контрагент_Key", "Запасы"],
                )
                _record_exact_line_allocations(
                    db,
                    group=group,
                    document=exact_doc or existing_doc,
                    generation_id=generation_id,
                )
                _upsert_purchase_links(
                    db,
                    group,
                    payload_hash=_group_export_payload_hash(
                        group, run_id=int(run.run_id), generation_id=int(generation_id),
                        freeze_version=int(run.active_freeze_version),
                    ),
                    target_ref_key=group.target_ref_key,
                    status="success",
                    last_error=None,
                    generation_id=generation_id,
                )
                continue

            min_need = min((date.fromisoformat(line.need_date) for line in group.lines if line.need_date), default=None)
            header_payload = {
                "Number": group.number,
                "Date": _fmt_1c_datetime(date.today()),
                "Posted": False,
                "Контрагент_Key": group.supplier_ref1c,
                "ДатаПоступления": _fmt_1c_datetime(min_need),
                "Комментарий": _order_comment(run_id, group),
                "Запасы": [],
            }
            header_payload["Запасы"] = _order_lines_payload("", group)
            created_header = create_purchase_order_document(client, header_payload)
            ref_key = str(created_header.get("Ref_Key") or "").strip()
            if not ref_key:
                raise RuntimeError(f"1C did not return Ref_Key for {group.number}")
            group.target_ref_key = ref_key
            exact_doc = created_header
            if not isinstance(exact_doc.get("Запасы"), list):
                exact_doc = (
                    _find_document_by_origin(
                        client,
                        entity=PURCHASE_ORDER_ENTITY,
                        token=_group_batch_token(group)[:32],
                        select_fields=["Ref_Key", "Контрагент_Key", "Запасы"],
                    )
                    or created_header
                )
            _record_exact_line_allocations(
                db,
                group=group,
                document=exact_doc,
                generation_id=generation_id,
            )
            _upsert_purchase_links(
                db,
                group,
                payload_hash=_group_export_payload_hash(
                    group, run_id=int(run.run_id), generation_id=int(generation_id),
                    freeze_version=int(run.active_freeze_version),
                ),
                target_ref_key=ref_key,
                status="success",
                last_error=None,
                generation_id=generation_id,
            )

            group.status = "created"
            created += 1
        except Exception as exc:
            group.status = "error"
            group.error = str(exc)
            _upsert_purchase_links(
                db,
                group,
                payload_hash=_group_export_payload_hash(
                    group, run_id=int(run.run_id), generation_id=int(generation_id),
                    freeze_version=int(run.active_freeze_version),
                ),
                target_ref_key=group.target_ref_key,
                status="error",
                last_error=group.error,
                generation_id=generation_id,
            )
            try:
                print(f"[1C purchase export] {group.number} failed: {group.error}")
            except Exception:
                pass

    db.commit()

    return {
        "status": "ok" if all(g.status != "error" for g in groups) else "partial_error",
        "dry_run": False,
        "orders_planned": len(groups),
        "orders_created": created,
        "orders_existing": existing,
        "lines_total": sum(len(g.lines) for g in groups),
        "skipped_rows": skipped_rows,
        "already_exported_purchase_ids": already_exported_ids,
        "orders": [asdict(g) for g in groups],
    }
