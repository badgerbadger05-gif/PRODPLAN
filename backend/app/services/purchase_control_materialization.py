"""Neutral purchase-control materialization API service.

Contract:
- Input is a selected set of ``row_keys`` from the current accepted
  ``purchase_control_journal`` snapshot.
- Read phase (``dry_run=True``) returns a deterministic preview and writes nothing.
- Write phase groups by supplier, validates all reservation lineage against the
  current snapshot generation, creates one immutable batch, and expects writer output
  to cover exactly the requested reservation quantities.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from datetime import date as _date_type
from typing import Any, Callable, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.services.one_c_export_common import (
    EMPTY_REF1C,
    add_origin_marker,
    clean_ref1c,
    create_odata_client,
    fmt_1c_datetime,
    find_document_by_origin,
    origin_token,
    payload_hash as _payload_hash,
)
from app.services.odata_config import load_odata_config as _load_odata_config
from app.services.one_c_purchase_order_export import (
    PURCHASE_ORDER_ENTITY,
    create_purchase_order_document,
)
from app.services.odata_client import OData1CClient
from app.services.planning_truth import PlanningTruthUnavailable
from . import purchase_control_snapshot
from .purchase_control_snapshot import validate_purchase_control_journal_buy_row


class PurchaseControlMaterializationError(ValueError):
    """Domain validation error for materialization requests."""


class PurchaseControlMaterializerNotConfigured(RuntimeError):
    """No production writer is configured for purchase-control materialization."""


@dataclass(frozen=True)
class ReservationAllocationLine:
    reservation_id: int
    allocated_qty: float
    supplier_order_ref: str
    supplier_order_line_no: str
    line_token: Optional[int] = None
    line_hash: Optional[str] = None


@dataclass
class MaterializeOrderLine:
    row_key: str
    reservation_id: int
    requirement_id: int
    item_id: int
    item_ref1c: str
    unit_ref1c: str
    planning_stock_pool: str
    destination_warehouse_ref1c: str
    need_date: Optional[str]
    order_date: Optional[str]
    qty: float
    request_line_token: Optional[int] = None
    request_line_hash: Optional[str] = None


@dataclass(frozen=True)
class MaterializeOrderGroup:
    supplier_id: int
    supplier_ref1c: str
    lines: list[MaterializeOrderLine]


# Injection point for production adapter.
MaterializerCallable = Callable[
    [Session, list[MaterializeOrderGroup], dict[str, Any], int, bool],
    Tuple[int, list[dict[str, Any]], dict[str, Any]],
]


_EPS = 1e-9
_EPS_ALLOC = 1e-6


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, (int, float)):
        return format(Decimal(str(value)).normalize(), "f") if value else "0"
    if isinstance(value, _date_type):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canonical_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(v) for v in value]
    return value


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        _canonical_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _positive_63bit_token(payload: dict[str, Any]) -> int:
    value = int.from_bytes(
        hashlib.sha256(
            json.dumps(
                _canonical_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).digest()[:8],
        "big",
    ) & ((1 << 63) - 1)
    return value or 1


def _acquire_reservation_claim_locks(
    db: Session,
    reservation_ids: Sequence[int],
) -> None:
    """Serialize durable claims in stable reservation order on PostgreSQL."""
    if db.get_bind().dialect.name != "postgresql":
        return
    for reservation_id in sorted({int(value) for value in reservation_ids}):
        lock_key = _positive_63bit_token(
            {
                "v": 1,
                "kind": "purchase_control_reservation_claim",
                "reservation_id": reservation_id,
            }
        )
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": lock_key},
        )


def _fmt_need_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return fmt_1c_datetime(date.fromisoformat(value))
    except ValueError:
        return None


def _stable_line_identity(
    line: MaterializeOrderLine,
    supplier_ref1c: str,
) -> dict[str, Any]:
    """Generation-independent identity of one fixed planning obligation."""
    return {
        "v": 5,
        "kind": "purchase_control_materialize_line",
        "row_key": line.row_key,
        "requirement_id": int(line.requirement_id),
        "supplier": clean_ref1c(supplier_ref1c),
        "item": line.item_ref1c,
        "unit": line.unit_ref1c,
        "planning_stock_pool": line.planning_stock_pool,
        "destination_warehouse_ref1c": line.destination_warehouse_ref1c,
        "qty": float(round(line.qty, 6)),
        "need_date": _normalize_text(line.need_date),
        "order_date": _normalize_text(line.order_date),
    }


def _one_c_line_payload_token_group_hash(group: MaterializeOrderGroup) -> str:
    """Stable supplier-group identity across snapshots and Ledger republishes."""
    payload = {
        "v": 5,
        "kind": "purchase_control_materialize_group",
        "supplier": clean_ref1c(group.supplier_ref1c),
        "lines": sorted(
            _canonical_hash(_stable_line_identity(line, group.supplier_ref1c))
            for line in group.lines
        ),
    }
    return _canonical_hash(payload)


def _group_marker_token(group: MaterializeOrderGroup) -> str:
    return origin_token(
        "purchase_control_materialization",
        {
            "supplier": clean_ref1c(group.supplier_ref1c),
            "group_hash": _one_c_line_payload_token_group_hash(group),
        },
    )


def _line_token_payload(
    *,
    line: MaterializeOrderLine,
    supplier_ref1c: str,
) -> dict[str, Any]:
    return _stable_line_identity(line, supplier_ref1c)


def _stamp_group_lines(group: MaterializeOrderGroup) -> None:
    lines = sorted(group.lines, key=lambda line: (line.row_key, line.reservation_id))
    seen: set[int] = set()
    for line in lines:
        payload = _line_token_payload(
            line=line,
            supplier_ref1c=group.supplier_ref1c,
        )
        token = _positive_63bit_token(payload)
        if token in seen:
            raise RuntimeError("purchase materialization line token collision")
        seen.add(token)
        line.request_line_token = token
        line.request_line_hash = _canonical_hash(payload)


def _supplier_order_comment(group: MaterializeOrderGroup) -> str:
    group_hash = _one_c_line_payload_token_group_hash(group)
    marker = _group_marker_token(group=group)
    return add_origin_marker(
        f"PRODPLAN source=purchase_control; group={group_hash}",
        marker,
    )


def _order_lines_payload(ref_key: str, group: MaterializeOrderGroup) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(group.lines, start=1):
        token = line.request_line_token
        if token is None:
            raise RuntimeError("1C purchase materialization requires stamped request line tokens")
        row = {
            "LineNumber": line_no,
            "Номенклатура_Key": line.item_ref1c,
            "Характеристика_Key": EMPTY_REF1C,
            "Количество": float(line.qty),
            "КлючСвязи": int(token),
            "ДатаПоступления": _fmt_need_date(line.need_date),
            "Содержание": "",
        }
        if clean_ref1c(line.unit_ref1c):
            row["ЕдиницаИзмерения"] = clean_ref1c(line.unit_ref1c)
            row["ЕдиницаИзмерения_Type"] = "StandardODATA.Catalog_КлассификаторЕдиницИзмерения"
        if ref_key:
            row["Ref_Key"] = ref_key
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows


def _parse_1c_need_date(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _verify_order_lines(
    *,
    doc: dict[str, Any],
    group: MaterializeOrderGroup,
) -> list[dict[str, Any]]:
    if not isinstance(doc, dict):
        raise RuntimeError("1C purchase materialization returned malformed document")

    supplier_ref = clean_ref1c(group.supplier_ref1c)
    doc_supplier_ref = clean_ref1c(doc.get("Контрагент_Key"))
    if doc_supplier_ref != supplier_ref:
        raise RuntimeError("1C purchase materialization returned supplier mismatch")

    lines = doc.get("Запасы")
    if not isinstance(lines, list) or len(lines) != len(group.lines):
        raise RuntimeError(
            "1C purchase materialization did not return exact order lines; "
            "cannot validate supplier order allocation"
        )

    supplier_order_ref = clean_ref1c(doc.get("Ref_Key"))
    if not supplier_order_ref:
        raise RuntimeError("1C purchase materialization returned no order ref")
    supplier_order_number = _normalize_text(doc.get("Number"))

    expected_by_token: dict[int, tuple[MaterializeOrderLine, str]] = {}
    for line in group.lines:
        if line.request_line_token is None:
            raise RuntimeError("1C purchase materialization received unstamped order lines")
        token = int(line.request_line_token)
        expected_by_token[token] = (
            line,
            _canonical_hash(
                _line_token_payload(
                    line=line,
                    supplier_ref1c=group.supplier_ref1c,
                )
            ),
        )

    exact_rows: list[tuple[str, int, str, int, str, MaterializeOrderLine]] = []
    for actual in lines:
        try:
            token = int(actual.get("КлючСвязи"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("1C purchase materialization returned malformed line token") from exc
        expected = expected_by_token.pop(token, None)
        if expected is None:
            raise RuntimeError("1C purchase materialization returned unknown or duplicate КлючСвязи")

        expected_line, expected_hash = expected
        line_no = actual.get("LineNumber")
        if line_no is None:
            raise RuntimeError("1C purchase materialization line has no exact LineNumber")
        actual_unit = clean_ref1c(actual.get("ЕдиницаИзмерения"))
        expected_unit = clean_ref1c(expected_line.unit_ref1c)
        raw_need = _parse_1c_need_date(actual.get("ДатаПоступления"))
        expected_need = _parse_1c_need_date(expected_line.need_date)
        if (
            _normalize_text(actual.get("Номенклатура_Key")) != expected_line.item_ref1c
            or _normalize_text(actual.get("Характеристика_Key")) != EMPTY_REF1C
            or abs(float(actual.get("Количество") or 0.0) - float(expected_line.qty)) > 1e-6
            or actual_unit != expected_unit
            or raw_need != (expected_need or "")
        ):
            raise RuntimeError("1C purchase materialization line payload mismatch")

        exact_rows.append(
            (
                supplier_order_ref,
                int(line_no),
                supplier_order_number,
                int(token),
                expected_hash,
                expected_line,
            )
        )

    if expected_by_token:
        raise RuntimeError("1C purchase materialization did not return every order line token")

    out: list[dict[str, Any]] = []
    for supplier_order_ref, line_no, order_number, token, line_hash, expected_line in exact_rows:
        out.append(
            {
                "reservation_id": int(expected_line.reservation_id),
                "supplier_order_ref": supplier_order_ref,
                "supplier_order_number": order_number,
                "supplier_order_line_no": str(line_no),
                "allocated_qty": float(round(expected_line.qty, 6)),
                "line_token": token,
                "line_hash": line_hash,
                "line_payload_hash": line_hash,
            }
        )
    return out


def _to_float(value: Any, *, field: str | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        if field:
            raise PurchaseControlMaterializationError(f"{field} is malformed") from exc
        raise PurchaseControlMaterializationError("numeric value is malformed") from exc
    if number != number:
        raise PurchaseControlMaterializationError(f"{field or 'value'} is malformed")
    return number


def _to_int(value: Any, *, field: str) -> int:
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise PurchaseControlMaterializationError(f"{field} is malformed") from exc
    if converted <= 0:
        raise PurchaseControlMaterializationError(f"{field} must be positive")
    return converted


def _optional_int(value: Any, *, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PurchaseControlMaterializationError(f"{field} is malformed")
    text = str(value).strip()
    if not text:
        return None
    return _to_int(text, field=field)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _row_key(row: Any) -> str:
    return _normalize_text(row.get("row_key"))


def _line_signature(rows: list[dict[str, Any]]) -> str:
    """Stable hash over requested lineages and allocation quantities."""

    signature_items: list[dict[str, Any]] = []
    for row in rows:
        row_key = _row_key(row)
        if not row_key:
            raise PurchaseControlMaterializationError("row_key is missing")
        slices = row.get("slices")
        if not isinstance(slices, list):
            raise PurchaseControlMaterializationError(f"row {row_key}: slices are malformed")
        for slice_row in slices:
            reservation_id = _to_int(
                slice_row.get("reservation_id"),
                field="reservation_id",
            )
            alloc_qty = round(_to_float(slice_row.get("to_order_qty"), field="to_order_qty"), 6)
            if alloc_qty <= 0:
                continue
            signature_items.append(
                {
                    "row_key": row_key,
                    "reservation_id": reservation_id,
                    "allocated_qty": alloc_qty,
                }
            )

    signature_items.sort(key=lambda item: (item["row_key"], item["reservation_id"]))
    return _payload_hash(signature_items)


def _materializer_not_configured(
    _db: Session,
    _groups: list[MaterializeOrderGroup],
    _request_payload: dict[str, Any],
    _batch_id: int,
    _dry_run: bool,
) -> Tuple[int, list[dict[str, Any]], dict[str, Any]]:
    raise PurchaseControlMaterializerNotConfigured(
        "purchase-control materialization writer is not configured"
    )


def _validate_requested_rows(
    snapshot: dict[str, Any],
    requested_keys: Sequence[str],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(snapshot, dict):
        raise PurchaseControlMaterializationError("snapshot is malformed")

    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise PurchaseControlMaterializationError("snapshot rows are malformed")

    by_key = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _row_key(row)
        if key:
            by_key[key] = row

    if not requested_keys:
        raise PurchaseControlMaterializationError("row_keys must be a non-empty list")

    normalized_keys: list[str] = []
    seen: set[str] = set()
    for raw_key in requested_keys:
        key = _normalize_text(raw_key)
        if not key:
            raise PurchaseControlMaterializationError("row_keys contains an empty value")
        if key in seen:
            raise PurchaseControlMaterializationError(f"row_key is duplicated: {key}")
        seen.add(key)
        normalized_keys.append(key)

    selected_rows: list[dict[str, Any]] = []
    for key in normalized_keys:
        row = by_key.get(key)
        if row is None:
            raise PurchaseControlMaterializationError(f"row_key not found: {key}")
        selected_rows.append(row)

    valid_rows: list[dict[str, Any]] = []
    for row in selected_rows:
        validate_purchase_control_journal_buy_row(row)
        if _row_key(row) == "":
            raise PurchaseControlMaterializationError("purchase control row has empty row_key")
        if _normalize_text(row.get("row_generator")) != "mrp_reservation":
            raise PurchaseControlMaterializationError(
                f"row {_row_key(row)} has unsupported generator"
            )
        to_order_qty = round(_to_float(row.get("to_order_qty"), field="to_order_qty"), 6)
        if to_order_qty > 0:
            valid_rows.append(row)

    if not valid_rows:
        raise PurchaseControlMaterializationError("no rows with positive to_order_qty")

    meta = snapshot.get("meta")
    if not isinstance(meta, dict):
        raise PurchaseControlMaterializationError("snapshot metadata is malformed")
    ledger_generation_id = int(_to_int(meta.get("ledger_generation"), field="ledger_generation"))

    return valid_rows, ledger_generation_id


def _load_groups_and_lineages(
    db: Session,
    snapshot: dict[str, Any],
    requested_keys: Sequence[str],
) -> tuple[list[MaterializeOrderGroup], list[dict[str, Any]], int]:
    selected_rows, ledger_generation_id = _validate_requested_rows(snapshot, requested_keys)
    configured_destination = clean_ref1c(
        _load_odata_config().get("purchase_destination_warehouse_ref1c")
    )

    line_rows: list[tuple[str, MaterializeOrderLine]] = []
    by_key = {_row_key(row): row for row in snapshot.get("rows", []) if isinstance(row, dict)}

    for row in selected_rows:
        row_key = _row_key(row)
        supplier_id = _to_int(row.get("supplier_id"), field="supplier_id")
        supplier = db.get(models.Supplier, supplier_id)
        if supplier is None:
            raise PurchaseControlMaterializationError(
                f"row {row_key}: supplier {supplier_id} not found"
            )
        supplier_ref1c = _normalize_text(supplier.supplier_ref1c)
        if not supplier_ref1c:
            raise PurchaseControlMaterializationError(
                f"row {row_key}: supplier {supplier_id} missing supplier_ref1c"
            )

        item_id = _to_int(row.get("item_id"), field="item_id")
        item = db.get(models.Item, item_id)
        if item is None:
            raise PurchaseControlMaterializationError(f"row {row_key}: item {item_id} not found")
        item_ref1c = _normalize_text(item.item_ref1c)
        if not item_ref1c:
            raise PurchaseControlMaterializationError(
                f"row {row_key}: item {item_id} missing item_ref1c"
            )

        unit_ref1c = _normalize_text(item.unit)
        if not unit_ref1c:
            raise PurchaseControlMaterializationError(
                f"row {row_key}: item {item_id} missing unit"
            )
        planning_stock_pool = _normalize_text(row.get("planning_stock_pool"))
        if not planning_stock_pool:
            raise PurchaseControlMaterializationError(
                f"row {row_key}: planning_stock_pool is missing"
            )
        destination_warehouse_ref1c = clean_ref1c(
            row.get("destination_warehouse_ref1c")
        ) or configured_destination

        slices = row.get("slices")
        if not isinstance(slices, list) or not slices:
            raise PurchaseControlMaterializationError(f"row {row_key} has no line slices")

        need_date = _normalize_text(row.get("need_date")) or None
        order_date = _normalize_text(row.get("plan_period_from")) or _normalize_text(row.get("order_date")) or None

        for slice_row in slices:
            if not isinstance(slice_row, dict):
                continue
            work_item_id = _to_int(
                slice_row.get("work_item_id"),
                field="work_item_id",
            )
            reservation_id = _to_int(
                slice_row.get("reservation_id"),
                field="reservation_id",
            )
            alloc_qty = round(
                _to_float(slice_row.get("to_order_qty"), field="to_order_qty"),
                6,
            )
            if alloc_qty <= 0:
                continue

            work_item = db.get(models.ReplenishmentWorkItem, work_item_id)
            if work_item is None:
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: work item {work_item_id} not found"
                )
            if int(work_item.ledger_generation_id) != ledger_generation_id:
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: work item {work_item_id} has stale generation"
                )
            if (
                int(work_item.reservation_id) != reservation_id
                or int(work_item.item_id) != item_id
                or str(work_item.replenishment_method) != "buy"
            ):
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: work item {work_item_id} lineage mismatch"
                )
            reservation = db.get(models.ReservationEntry, reservation_id)
            if reservation is None:
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: reservation {reservation_id} not found"
                )
            if reservation.ledger_generation_id != ledger_generation_id:
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: reservation {reservation_id} has stale generation"
                )
            if reservation.item_id != item_id:
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: reservation {reservation_id} item mismatch"
                )
            if str(reservation.realization_mode) != "buy":
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: reservation {reservation_id} is not a buy reservation"
                )
            if reservation.run_id is None:
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: reservation {reservation_id} has no planning run lineage"
                )
            replenishment_open = float(
                work_item.replenishment_remaining_qty or 0
            )
            if replenishment_open + _EPS < alloc_qty:
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: reservation {reservation_id} "
                    "replenishment remainder is too small"
                )

            row_need_date = _normalize_text(slice_row.get("plan_period_to")) or need_date
            line_rows.append(
                (
                    row_key,
                    MaterializeOrderLine(
                        row_key=row_key,
                        reservation_id=reservation_id,
                        requirement_id=int(reservation.requirement_id),
                        item_id=item_id,
                        item_ref1c=item_ref1c,
                        unit_ref1c=unit_ref1c,
                        planning_stock_pool=planning_stock_pool,
                        destination_warehouse_ref1c=destination_warehouse_ref1c,
                        need_date=row_need_date,
                        order_date=order_date,
                        qty=alloc_qty,
                    ),
                )
            )

    if not line_rows:
        raise PurchaseControlMaterializationError("no allocation slices with positive to_order_qty")

    groups: dict[int, MaterializeOrderGroup] = {}
    for _line_row_key, line in line_rows:
        _ = _line_row_key
        row = by_key.get(line.row_key)
        if row is None:
            raise PurchaseControlMaterializationError(
                f"row {line.row_key}: row disappeared during materialization"
            )
        supplier_id = _to_int(row.get("supplier_id"), field="supplier_id")
        supplier = db.get(models.Supplier, supplier_id)
        if supplier is None:
            raise PurchaseControlMaterializationError(
                f"row {line.row_key}: supplier {supplier_id} disappeared during materialization"
            )
        supplier_ref1c = _normalize_text(supplier.supplier_ref1c)
        if not supplier_ref1c:
            raise PurchaseControlMaterializationError(
                f"row {line.row_key}: supplier {supplier_id} missing supplier_ref1c"
            )

        group = groups.get(supplier_id)
        if group is None:
            group = MaterializeOrderGroup(
                supplier_id=supplier_id,
                supplier_ref1c=supplier_ref1c,
                lines=[],
            )
            groups[supplier_id] = group
        group.lines.append(line)

    for group in groups.values():
        group.lines.sort(key=lambda line: (line.item_ref1c, line.item_id, line.reservation_id))

    return list(groups.values()), selected_rows, ledger_generation_id


def _build_request_payload(
    *,
    snapshot: dict[str, Any],
    requested_keys: Sequence[str],
    selected_rows: list[dict[str, Any]],
    groups: Sequence[MaterializeOrderGroup],
    ledger_generation_id: int,
) -> dict[str, Any]:
    return {
        "source": "purchase_control_snapshot",
        "snapshot_id": int(snapshot.get("meta", {}).get("snapshot_id") or 0),
        "snapshot_ledger_generation": int(ledger_generation_id),
        "request_hash": _payload_hash(
            {
                "snapshot_id": int(snapshot.get("meta", {}).get("snapshot_id") or 0),
                "rows": _line_signature(selected_rows),
            }
        ),
        "row_count": len(selected_rows),
        "row_keys": sorted(set(_normalize_text(k) for k in requested_keys)),
        "groups": [
            {
                "supplier_id": int(group.supplier_id),
                "supplier_ref1c": group.supplier_ref1c,
                "line_count": len(group.lines),
                "lines": [
                    {
                        "row_key": line.row_key,
                        "reservation_id": int(line.reservation_id),
                        "item_id": int(line.item_id),
                        "item_ref1c": line.item_ref1c,
                        "unit_ref1c": line.unit_ref1c,
                        "need_date": line.need_date,
                        "order_date": line.order_date,
                        "qty": float(line.qty),
                    }
                    for line in group.lines
                ],
            }
            for group in sorted(groups, key=lambda group: group.supplier_id)
        ],
    }


def _make_idempotency_key(
    groups: Sequence[MaterializeOrderGroup],
) -> str:
    canonical = {
        "v": 2,
        "kind": "purchase_control_materialize",
        "groups": [
            {
                "supplier": clean_ref1c(group.supplier_ref1c),
                "lines": sorted(
                    _canonical_hash(
                        _stable_line_identity(line, group.supplier_ref1c)
                    )
                    for line in group.lines
                ),
            }
            for group in sorted(groups, key=lambda value: value.supplier_ref1c)
        ],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"purchase-control-materialize:{digest[:32]}"


def _parse_allocation_record(
    allocation: dict[str, Any],
    expected: dict[int, float],
    seen: dict[int, float],
) -> tuple[int, float, str, str, Optional[str], Optional[int], Optional[str]]:
    if not isinstance(allocation, dict):
        raise PurchaseControlMaterializationError("allocation item must be an object")

    rid = _to_int(allocation.get("reservation_id"), field="allocation.reservation_id")
    if rid not in expected:
        raise PurchaseControlMaterializationError(f"unexpected allocation reservation_id={rid}")

    allocated_qty = round(_to_float(allocation.get("allocated_qty"), field="allocation.allocated_qty"), 6)
    if allocated_qty <= 0:
        raise PurchaseControlMaterializationError(
            f"allocation for reservation_id={rid} must be positive"
        )

    supplier_order_ref = _normalize_text(allocation.get("supplier_order_ref"))
    supplier_order_line_no = _normalize_text(allocation.get("supplier_order_line_no"))
    if not supplier_order_ref or not supplier_order_line_no:
        raise PurchaseControlMaterializationError(
            f"allocation for reservation_id={rid} misses supplier order identity"
        )

    order_number = _normalize_text(allocation.get("supplier_order_number")) or None

    token = _optional_int(allocation.get("line_token"), field="allocation.line_token")
    line_hash = _normalize_text(allocation.get("line_hash")) or None

    expected_qty = expected[rid]
    seen[rid] = round(seen.get(rid, 0.0) + allocated_qty, 6)
    if seen[rid] - expected_qty > _EPS_ALLOC:
        raise PurchaseControlMaterializationError(f"allocation overflow for reservation_id={rid}")

    return (
        rid,
        allocated_qty,
        supplier_order_ref,
        supplier_order_line_no,
        order_number,
        token,
        line_hash,
    )


def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _build_writer_result(
    expected: dict[int, float],
    seen: dict[int, float],
    allocations_by_order: dict[str, list[dict[str, Any]]],
    result_payload: Any,
    created_qty: int,
) -> dict[str, Any]:
    allocation_lines: list[dict[str, Any]] = []
    for reservation_id in sorted(expected):
        allocation_lines.append(
            {
                "reservation_id": int(reservation_id),
                "allocated_qty": float(round(seen[reservation_id], 6)),
            }
        )

    return {
        "status": "ok",
        "orders_created": int(created_qty),
        "allocations": allocation_lines,
        "orders_by_supplier": {
            order_ref: [
                {
                    "supplier_order_line_no": alloc["supplier_order_line_no"],
                    "reservation_id": int(alloc["reservation_id"]),
                    "allocated_qty": float(alloc["allocated_qty"]),
                    "line_token": alloc.get("line_token"),
                    "line_hash": alloc.get("line_hash"),
                }
                for alloc in lines
            ]
            for order_ref, lines in allocations_by_order.items()
        },
        "writer_result": result_payload,
    }


def _build_allocation_records(
    allocations: list[dict[str, Any]],
    expected: dict[int, float],
    line_by_reservation: dict[int, MaterializeOrderLine],
    batch_id: int,
    ledger_generation_id: int,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    seen: dict[int, float] = {}
    by_order_ref: dict[str, list[dict[str, Any]]] = {}
    db_records: list[models.PurchaseExportObligationAllocation] = []
    allocation_lines: list[ReservationAllocationLine] = []

    for allocation in allocations:
        (
            reservation_id,
            allocated_qty,
            supplier_order_ref,
            supplier_order_line_no,
            supplier_order_number,
            line_token,
            line_hash,
        ) = _parse_allocation_record(allocation, expected, seen)

        line = line_by_reservation.get(reservation_id)
        if line is None:
            raise PurchaseControlMaterializationError(
                f"allocation includes unsupported reservation_id={reservation_id}"
            )

        db_records.append(
            models.PurchaseExportObligationAllocation(
                batch_id=int(batch_id),
                reservation_id=reservation_id,
                supplier_order_ref=supplier_order_ref,
                supplier_order_line_no=supplier_order_line_no,
                line_token=line_token,
                line_hash=line_hash,
                allocated_qty=Decimal(str(allocated_qty)),
                ledger_generation_id=int(ledger_generation_id),
                item_id=int(line.item_id),
                planning_stock_pool=_normalize_text(line.planning_stock_pool),
                destination_warehouse_ref1c=clean_ref1c(line.destination_warehouse_ref1c),
                eta_date=_parse_iso_date(line.need_date),
                planned_purchase_id=_optional_int(
                    allocation.get("planned_purchase_id"),
                    field="allocation.planned_purchase_id",
                ),
            )
        )

        by_order_ref.setdefault(supplier_order_ref, [])
        by_order_ref[supplier_order_ref].append(
            {
                "supplier_order_line_no": supplier_order_line_no,
                "reservation_id": reservation_id,
                "allocated_qty": allocated_qty,
                "line_token": line_token,
                "line_hash": line_hash,
                "supplier_order_number": supplier_order_number,
            }
        )
        allocation_lines.append(
            ReservationAllocationLine(
                reservation_id=reservation_id,
                allocated_qty=allocated_qty,
                supplier_order_ref=supplier_order_ref,
                supplier_order_line_no=supplier_order_line_no,
                line_token=line_token,
                line_hash=line_hash,
            )
        )

    expected_ids = sorted(expected)
    missing = [rid for rid in expected_ids if rid not in seen]
    if missing:
        raise PurchaseControlMaterializationError(
            f"materializer omitted reservations: {missing}"
        )

    mismatch = any(abs(seen[rid] - expected[rid]) > _EPS_ALLOC for rid in expected_ids)
    if mismatch:
        raise PurchaseControlMaterializationError("materializer allocations mismatch")

    return {
        "allocations": allocation_lines,
        "records": db_records,
        "by_order_ref": by_order_ref,
        "seen": seen,
    }


def _flatten_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise PurchaseControlMaterializationError("snapshot row is malformed")
    return dict(row)


def _materialize_purchase_control_orders_to_1c(
    db: Session,
    groups: list[MaterializeOrderGroup],
    request_payload: dict[str, Any],
    _batch_id: int,
    _dry_run: bool,
) -> tuple[int, list[dict[str, Any]], dict[str, Any]]:
    if _dry_run:
        return 0, [], {"status": "ok", "orders": 0, "dry_run": True}

    if not groups:
        return 0, [], {"status": "ok", "orders": 0}

    _ = db  # reader-only
    configured_destination = clean_ref1c(
        _load_odata_config().get("purchase_destination_warehouse_ref1c")
    )
    for group in groups:
        for line in group.lines:
            if not clean_ref1c(line.destination_warehouse_ref1c):
                line.destination_warehouse_ref1c = configured_destination
    missing_destination = [
        int(line.reservation_id)
        for group in groups
        for line in group.lines
        if not clean_ref1c(line.destination_warehouse_ref1c)
    ]
    if missing_destination:
        raise PurchaseControlMaterializationError(
            "purchase_destination_warehouse_ref1c is required for BUY materialization; "
            f"reservations={sorted(missing_destination)}"
        )
    for group in groups:
        _stamp_group_lines(group)

    client = create_odata_client(
        _load_odata_config(), OData1CClient, allow_production=False, require_demo_base=True
    )

    created = 0
    all_allocations: list[dict[str, Any]] = []
    for group in sorted(groups, key=lambda g: g.supplier_id):
        order_comment = _supplier_order_comment(group)
        group_hash = _one_c_line_payload_token_group_hash(group)
        header_payload = {
            "Number": f"PC-{group.supplier_id}-{group_hash[:8]}",
            "Date": fmt_1c_datetime(date.today()),
            "Posted": False,
            "Контрагент_Key": clean_ref1c(group.supplier_ref1c),
            "ДатаПоступления": _fmt_need_date(_normalize_text(group.lines[0].need_date)),
            "Комментарий": order_comment,
            "Запасы": _order_lines_payload("", group),
        }

        token = _group_marker_token(group=group)
        existing = find_document_by_origin(
            client,
            entity=PURCHASE_ORDER_ENTITY,
            token=token,
            select_fields=["Ref_Key", "Number", "Контрагент_Key", "Комментарий", "Запасы"],
        )

        if existing:
            doc = existing
            doc_token = _verify_order_lines(doc=doc, group=group)
            for allocation in doc_token:
                all_allocations.append(allocation)
            continue

        created_doc = create_purchase_order_document(client, header_payload)
        ref_key = clean_ref1c(created_doc.get("Ref_Key") if isinstance(created_doc, dict) else None)
        if not ref_key:
            raise RuntimeError("1C purchase materialization did not return order Ref_Key")

        recovered = client.get_all(
            PURCHASE_ORDER_ENTITY,
            filter_query=f"substringof('{token}', Комментарий)",
            select_fields=["Ref_Key", "Контрагент_Key", "Запасы"],
            top=1,
            max_records=2,
            max_pages=1,
            order_by=None,
        )
        if len(recovered) > 1:
            raise RuntimeError("1C purchase materialization origin marker is ambiguous")
        if recovered:
            doc = recovered[0]
        else:
            doc = {"Ref_Key": ref_key, **created_doc}

        if not isinstance(doc, dict):
            raise RuntimeError("1C purchase materialization returned malformed order document")

        doc_token = _verify_order_lines(doc=doc, group=group)
        for allocation in doc_token:
            all_allocations.append(allocation)
        created += 1

    return created, all_allocations, {"status": "ok", "orders": created}


def _resolve_default_materializer() -> MaterializerCallable:
    config = _load_odata_config()
    if not isinstance(config, dict) or not str(config.get("base_url") or "").strip():
        return _materializer_not_configured
    return _materialize_purchase_control_orders_to_1c


_BUY_RESERVATION_SOURCE_DOCTYPE = "buy_reservation"


def _reservation_claim_hash(
    group: MaterializeOrderGroup,
    reservation_id: int,
    batch_id: int,
) -> str:
    return _canonical_hash(
        {
            "v": 1,
            "kind": "buy_reservation_claim",
            "reservation_id": int(reservation_id),
            "batch_id": int(batch_id),
            "lines": sorted(
                _canonical_hash(
                    _stable_line_identity(line, group.supplier_ref1c)
                )
                for line in group.lines
                if int(line.reservation_id) == int(reservation_id)
            ),
        }
    )


def _group_reservation_ids(group: MaterializeOrderGroup) -> set[int]:
    return {int(line.reservation_id) for line in group.lines}


def _preflight_existing_claims(
    db: Session,
    groups: Sequence[MaterializeOrderGroup],
    *,
    allowed_success_ids: set[int] | None = None,
) -> None:
    """Reject a stale/overlapping selection before any new external write."""
    reservation_ids = sorted(
        {
            int(line.reservation_id)
            for group in groups
            for line in group.lines
        }
    )
    if not reservation_ids:
        return
    links = (
        db.query(models.SyncLink)
        .filter(
            models.SyncLink.source_system == "PRODPLAN",
            models.SyncLink.source_doctype == _BUY_RESERVATION_SOURCE_DOCTYPE,
            models.SyncLink.source_id.in_(reservation_ids),
            models.SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
            models.SyncLink.status == "success",
        )
        .all()
    )
    allowed = allowed_success_ids or set()
    conflicts = [link for link in links if int(link.source_id) not in allowed]
    if conflicts:
        raise PurchaseControlMaterializationError(
            "selection contains already materialized BUY reservations: "
            f"{sorted(int(link.source_id) for link in conflicts)}"
        )


def _claim_group(
    db: Session,
    group: MaterializeOrderGroup,
    ledger_generation_id: int,
    batch_id: int,
) -> list[models.SyncLink]:
    """Create durable reservation-level claims before recover/POST."""
    claimed: list[models.SyncLink] = []
    _acquire_reservation_claim_locks(db, sorted(_group_reservation_ids(group)))
    for reservation_id in sorted(_group_reservation_ids(group)):
        claim_hash = _reservation_claim_hash(group, reservation_id, batch_id)
        link = (
            db.query(models.SyncLink)
            .filter_by(
                source_system="PRODPLAN",
                source_doctype=_BUY_RESERVATION_SOURCE_DOCTYPE,
                source_id=int(reservation_id),
                target_entity=PURCHASE_ORDER_ENTITY,
            )
            .one_or_none()
        )
        if link is None:
            link = models.SyncLink(
                source_system="PRODPLAN",
                source_doctype=_BUY_RESERVATION_SOURCE_DOCTYPE,
                source_id=int(reservation_id),
                target_system="1C",
                target_entity=PURCHASE_ORDER_ENTITY,
                payload_hash=claim_hash,
                target_ref_key=None,
                target_number="",
                ledger_generation_id=int(ledger_generation_id),
                status="planned",
            )
            db.add(link)
        else:
            if str(link.payload_hash or "") != claim_hash:
                raise PurchaseControlMaterializationError(
                    f"BUY reservation {reservation_id} claim payload changed"
                )
            if str(link.status) == "success":
                raise PurchaseControlMaterializationError(
                    f"BUY reservation {reservation_id} is already materialized"
                )
            link.status = "planned"
            link.last_error = None
            link.ledger_generation_id = int(ledger_generation_id)
            link.payload_hash = claim_hash
        claimed.append(link)
    db.flush()
    return claimed


def _complete_group_claims(
    claims: Sequence[models.SyncLink],
    allocations: Sequence[dict[str, Any]],
) -> None:
    refs_by_reservation: dict[int, set[str]] = {}
    numbers_by_reservation: dict[int, set[str]] = {}
    for allocation in allocations:
        reservation_id = _to_int(
            allocation.get("reservation_id"),
            field="allocation.reservation_id",
        )
        order_ref = clean_ref1c(allocation.get("supplier_order_ref"))
        if order_ref:
            refs_by_reservation.setdefault(reservation_id, set()).add(order_ref)
        order_number = _normalize_text(allocation.get("supplier_order_number"))
        if order_number:
            numbers_by_reservation.setdefault(reservation_id, set()).add(order_number)
    now = datetime.now(timezone.utc)
    for link in claims:
        refs = refs_by_reservation.get(int(link.source_id), set())
        if len(refs) != 1:
            raise PurchaseControlMaterializationError(
                f"BUY reservation {link.source_id} does not map to one supplier order"
            )
        link.target_ref_key = next(iter(refs))
        numbers = numbers_by_reservation.get(int(link.source_id), set())
        if numbers:
            link.target_number = next(iter(numbers))
        link.status = "success"
        link.last_error = None
        link.last_synced_at = now


def _fail_group_claims(
    claims: Sequence[models.SyncLink],
    reason: str,
) -> None:
    for link in claims:
        if str(link.status) != "success":
            link.status = "error"
            link.last_error = reason


def materialize_rows(
    db: Session,
    *,
    snapshot_id: int,
    row_keys: Sequence[str],
    dry_run: bool = False,
    materializer: Optional[MaterializerCallable] = None,
) -> dict[str, Any]:
    try:
        snapshot = purchase_control_snapshot.read_snapshot(db)
    except PlanningTruthUnavailable as exc:
        raise PurchaseControlSnapshotUnavailable(str(exc)) from exc

    meta = snapshot.get("meta")
    if not isinstance(meta, dict):
        raise PurchaseControlMaterializationError("snapshot metadata is malformed")
    current_snapshot_id = int(_to_int(meta.get("snapshot_id"), field="snapshot_id"))
    if int(snapshot_id) != current_snapshot_id:
        raise PurchaseControlMaterializationError(
            "requested snapshot_id does not match current accepted purchase-control snapshot"
        )

    groups, selected_rows, ledger_generation_id = _load_groups_and_lineages(
        db, snapshot, row_keys
    )
    key = _make_idempotency_key(groups)
    request_payload = _build_request_payload(
        snapshot=snapshot,
        requested_keys=row_keys,
        selected_rows=selected_rows,
        groups=groups,
        ledger_generation_id=ledger_generation_id,
    )
    request_hash = _payload_hash(request_payload)

    selected_rows = [_flatten_row(row) for row in selected_rows]
    snapshot_rows = [_flatten_row(row) for row in selected_rows]

    preview = {
        "snapshot_id": current_snapshot_id,
        "ledger_generation_id": int(meta.get("ledger_generation") or ledger_generation_id),
        "dry_run": bool(dry_run),
        "idempotency_key": key,
        "snapshot_meta": {
            "truth_status": meta.get("truth_status"),
            "ledger_generation": meta.get("ledger_generation"),
            "cutoff": meta.get("cutoff"),
        },
        "rows": snapshot_rows,
        "rows_total": len(snapshot_rows),
        "supplier_groups": [
            {
                "supplier_id": group.supplier_id,
                "supplier_ref1c": group.supplier_ref1c,
                "line_count": len(group.lines),
            }
            for group in sorted(groups, key=lambda group: group.supplier_id)
        ],
        "request_hash": request_hash,
        "row_keys": sorted({_normalize_text(key) for key in row_keys}),
    }

    if dry_run:
        return preview

    writer = materializer if materializer is not None else _resolve_default_materializer()
    if writer is _materializer_not_configured:
        writer(db, groups, request_payload, 0, dry_run)

    batch_id: int | None = None
    active_claims: list[models.SyncLink] = []
    try:
        batch = (
            db.query(models.PurchaseExportBatch)
            .filter_by(idempotency_key=key)
            .one_or_none()
        )
        if batch is not None and batch.status == "completed":
            return {
                **preview,
                "batch_id": int(batch.id),
                "status": batch.status,
                "result": batch.result_payload,
                "created_at": batch.created_at.isoformat() if batch.created_at else None,
                "completed_at": batch.completed_at.isoformat()
                if batch.completed_at
                else None,
            }
        if batch is not None and batch.status == "aborted":
            raise PurchaseControlMaterializationError(
                "aborted materialization batch requires manual resolution"
            )
        if batch is None:
            batch = models.PurchaseExportBatch(
                ledger_generation_id=int(ledger_generation_id),
                planning_read_snapshot_id=int(meta.get("snapshot_id") or current_snapshot_id),
                idempotency_key=key,
                status="building",
                payload_hash=request_hash,
                request_payload=request_payload,
                result_payload=None,
            )
            db.add(batch)
            db.flush()
        else:
            batch.status = "building"
            batch.reason = None
            batch.completed_at = None
            batch.result_payload = None
        batch_id = int(batch.id)

        completed_ids = {
            int(value)
            for (value,) in (
                db.query(models.PurchaseExportObligationAllocation.reservation_id)
                .filter_by(batch_id=batch_id)
                .distinct()
                .all()
            )
        }
        _preflight_existing_claims(
            db,
            groups,
            allowed_success_ids=completed_ids,
        )
        pending_groups = [
            group
            for group in sorted(groups, key=lambda value: value.supplier_id)
            if not _group_reservation_ids(group).issubset(completed_ids)
        ]
        db.commit()

        total_created = 0
        writer_results: list[dict[str, Any]] = []

        for group in pending_groups:
            active_claims = _claim_group(
                db,
                group,
                ledger_generation_id,
                batch_id,
            )
            db.commit()

            created_qty, allocations_raw, writer_result = writer(
                db,
                [group],
                request_payload,
                batch_id,
                dry_run,
            )
            if not isinstance(created_qty, int) or created_qty < 0:
                raise PurchaseControlMaterializationError(
                    "materializer returned malformed created_qty"
                )
            if not isinstance(allocations_raw, list):
                raise PurchaseControlMaterializationError(
                    "materializer returned malformed allocations"
                )

            expected: dict[int, float] = {}
            for line in group.lines:
                reservation_id = int(line.reservation_id)
                expected[reservation_id] = round(
                    float(expected.get(reservation_id, 0.0))
                    + float(round(line.qty, 6)),
                    6,
                )
            allocation_payload = _build_allocation_records(
                [dict(value) for value in allocations_raw],
                expected,
                {int(line.reservation_id): line for line in group.lines},
                batch_id,
                ledger_generation_id,
            )
            for record in allocation_payload["records"]:
                db.add(record)
            _complete_group_claims(
                active_claims,
                allocations_raw,
            )
            db.commit()
            active_claims = []

            total_created += created_qty
            writer_results.append(dict(writer_result or {}))

        all_allocations = (
            db.query(models.PurchaseExportObligationAllocation)
            .filter_by(batch_id=batch_id)
            .all()
        )
        expected_all: dict[int, float] = {}
        seen_all: dict[int, float] = {}
        by_order_all: dict[str, list[dict[str, Any]]] = {}
        for group in groups:
            for line in group.lines:
                reservation_id = int(line.reservation_id)
                expected_all[reservation_id] = round(
                    expected_all.get(reservation_id, 0.0) + float(line.qty),
                    6,
                )
        for allocation in all_allocations:
            reservation_id = int(allocation.reservation_id)
            seen_all[reservation_id] = round(
                seen_all.get(reservation_id, 0.0) + float(allocation.allocated_qty),
                6,
            )
            by_order_all.setdefault(str(allocation.supplier_order_ref), []).append(
                {
                    "supplier_order_line_no": str(allocation.supplier_order_line_no),
                    "reservation_id": reservation_id,
                    "allocated_qty": float(allocation.allocated_qty),
                    "line_token": allocation.line_token,
                    "line_hash": allocation.line_hash,
                }
            )
        if expected_all != seen_all:
            raise PurchaseControlMaterializationError(
                "durable materialization allocations mismatch"
            )

        batch = db.get(models.PurchaseExportBatch, batch_id)
        if batch is None:
            raise PurchaseControlMaterializationError("materialization batch disappeared")
        result = _build_writer_result(
            expected=expected_all,
            seen=seen_all,
            allocations_by_order=by_order_all,
            result_payload={"groups": writer_results},
            created_qty=total_created,
        )
        batch.status = "completed"
        batch.completed_at = datetime.now(timezone.utc)
        batch.result_payload = result
        db.commit()
        return {
            **preview,
            "batch_id": batch_id,
            "status": batch.status,
            "result": batch.result_payload,
        }
    except Exception as exc:  # noqa: BLE001
        if not db.is_active:
            db.rollback()
        if batch_id is not None:
            batch = db.get(models.PurchaseExportBatch, batch_id)
            if batch is not None:
                batch.status = "failed"
                batch.reason = str(exc)
                batch.completed_at = datetime.now(timezone.utc)
                batch.result_payload = {"status": "failed", "reason": str(exc)}
            if active_claims:
                claim_ids = [int(link.link_id) for link in active_claims if link.link_id]
                durable_claims = (
                    db.query(models.SyncLink)
                    .filter(models.SyncLink.link_id.in_(claim_ids))
                    .all()
                )
                _fail_group_claims(durable_claims, str(exc))
            db.commit()
        raise


class PurchaseControlSnapshotUnavailable(RuntimeError):
    def __init__(self, reason: str):
        self.detail = {
            "code": "purchase_control_snapshot_unavailable",
            "reason": reason,
            "consumer": "purchase_control_journal",
        }
        super().__init__(reason)
