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
from app.services.one_c_purchase_order_export import PURCHASE_ORDER_ENTITY
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
    item_id: int
    item_ref1c: str
    unit_ref1c: str
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


def _acquire_materialization_lock(db: Session, idempotency_key: str) -> None:
    """Serialize the local claim and the external 1C call for one request."""
    if db.get_bind().dialect.name != "postgresql":
        return
    raw = hashlib.sha256(idempotency_key.encode("utf-8")).digest()[:8]
    lock_key = int.from_bytes(raw, "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})


def _fmt_need_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return fmt_1c_datetime(date.fromisoformat(value))
    except ValueError:
        return None


def _one_c_line_payload_token_group_hash(group: MaterializeOrderGroup) -> str:
    """Stable hash payload for a supplier group across instances."""
    reservation_ids = sorted(int(line.reservation_id) for line in group.lines)
    payload = {
        "v": 4,
        "kind": "purchase_control_materialize_group",
        "supplier": clean_ref1c(group.supplier_ref1c),
        "reservation_ids": reservation_ids,
    }
    return _canonical_hash(payload)


def _group_marker_token(request_hash: str, group: MaterializeOrderGroup) -> str:
    return origin_token(
        "purchase_control_materialization",
        {
            "request_hash": request_hash,
            "supplier": clean_ref1c(group.supplier_ref1c),
            "group_hash": _one_c_line_payload_token_group_hash(group),
        },
    )


def _line_token_payload(
    *,
    line: MaterializeOrderLine,
    request_hash: str,
    supplier_ref1c: str,
) -> dict[str, Any]:
    return {
        "v": 4,
        "kind": "purchase_control_materialize_line",
        "request_hash": request_hash,
        "row_key": line.row_key,
        "supplier": clean_ref1c(supplier_ref1c),
        "reservation_id": int(line.reservation_id),
        "item": line.item_ref1c,
        "unit": line.unit_ref1c,
        "qty": float(round(line.qty, 6)),
        "need_date": _normalize_text(line.need_date),
        "order_date": _normalize_text(line.order_date),
    }


def _stamp_group_lines(group: MaterializeOrderGroup, request_hash: str) -> None:
    lines = sorted(group.lines, key=lambda line: (line.row_key, line.reservation_id))
    seen: set[int] = set()
    for line in lines:
        payload = _line_token_payload(
            line=line,
            request_hash=request_hash,
            supplier_ref1c=group.supplier_ref1c,
        )
        token = _positive_63bit_token(payload)
        if token in seen:
            raise RuntimeError("purchase materialization line token collision")
        seen.add(token)
        line.request_line_token = token
        line.request_line_hash = _canonical_hash(payload)


def _supplier_order_comment(request_hash: str, group: MaterializeOrderGroup) -> str:
    marker = _group_marker_token(request_hash=request_hash, group=group)
    return add_origin_marker(f"PRODPLAN source=purchase_control; {request_hash}", marker)


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
    request_hash: str,
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
                    request_hash=request_hash,
                    supplier_ref1c=group.supplier_ref1c,
                )
            ),
        )

    exact_rows: list[tuple[str, int, int, str, MaterializeOrderLine]] = []
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
                int(token),
                expected_hash,
                expected_line,
            )
        )

    if expected_by_token:
        raise RuntimeError("1C purchase materialization did not return every order line token")

    out: list[dict[str, Any]] = []
    for supplier_order_ref, line_no, token, line_hash, expected_line in exact_rows:
        out.append(
            {
                "reservation_id": int(expected_line.reservation_id),
                "supplier_order_ref": supplier_order_ref,
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

        slices = row.get("slices")
        if not isinstance(slices, list) or not slices:
            raise PurchaseControlMaterializationError(f"row {row_key} has no line slices")

        need_date = _normalize_text(row.get("need_date")) or None
        order_date = _normalize_text(row.get("plan_period_from")) or _normalize_text(row.get("order_date")) or None

        for slice_row in slices:
            if not isinstance(slice_row, dict):
                continue
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
            if float(reservation.uncovered_qty or 0) + _EPS < alloc_qty:
                raise PurchaseControlMaterializationError(
                    f"row {row_key}: reservation {reservation_id} uncovered qty is too small"
                )

            row_need_date = _normalize_text(slice_row.get("plan_period_to")) or need_date
            line_rows.append(
                (
                    row_key,
                    MaterializeOrderLine(
                        row_key=row_key,
                        reservation_id=reservation_id,
                        item_id=item_id,
                        item_ref1c=item_ref1c,
                        unit_ref1c=unit_ref1c,
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
    snapshot: dict[str, Any],
    requested_keys: Sequence[str],
    selected_rows: list[dict[str, Any]],
) -> str:
    canonical = {
        "snapshot_id": int(snapshot.get("meta", {}).get("snapshot_id") or 0),
        "rows": _line_signature(selected_rows),
        "request_keys": sorted({_normalize_text(key) for key in requested_keys}),
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
) -> tuple[int, float, str, str, Optional[int], Optional[str]]:
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

    token = _optional_int(allocation.get("line_token"), field="allocation.line_token")
    line_hash = _normalize_text(allocation.get("line_hash")) or None

    expected_qty = expected[rid]
    seen[rid] = round(seen.get(rid, 0.0) + allocated_qty, 6)
    if seen[rid] - expected_qty > _EPS_ALLOC:
        raise PurchaseControlMaterializationError(f"allocation overflow for reservation_id={rid}")

    return rid, allocated_qty, supplier_order_ref, supplier_order_line_no, token, line_hash


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
    batch_id: int,
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
            line_token,
            line_hash,
        ) = _parse_allocation_record(allocation, expected, seen)

        db_records.append(
            models.PurchaseExportObligationAllocation(
                batch_id=int(batch_id),
                reservation_id=reservation_id,
                supplier_order_ref=supplier_order_ref,
                supplier_order_line_no=supplier_order_line_no,
                line_token=line_token,
                line_hash=line_hash,
                allocated_qty=Decimal(str(allocated_qty)),
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
    request_hash = _normalize_text(request_payload.get("request_hash"))
    if not request_hash:
        request_hash = _payload_hash(request_payload)

    for group in groups:
        _stamp_group_lines(group, request_hash)

    client = create_odata_client(
        _load_odata_config(), OData1CClient, allow_production=False, require_demo_base=True
    )

    created = 0
    all_allocations: list[dict[str, Any]] = []
    for group in sorted(groups, key=lambda g: g.supplier_id):
        order_comment = _supplier_order_comment(request_hash, group)
        header_payload = {
            "Number": f"PC-{group.supplier_id}-{request_hash[:8]}",
            "Date": fmt_1c_datetime(date.today()),
            "Posted": False,
            "Контрагент_Key": clean_ref1c(group.supplier_ref1c),
            "ДатаПоступления": _fmt_need_date(_normalize_text(group.lines[0].need_date)),
            "Комментарий": order_comment,
            "Запасы": _order_lines_payload("", group),
        }

        token = _group_marker_token(request_hash=request_hash, group=group)
        existing = find_document_by_origin(
            client,
            entity=PURCHASE_ORDER_ENTITY,
            token=token,
            select_fields=["Ref_Key", "Контрагент_Key", "Комментарий", "Запасы"],
        )

        if existing:
            doc = existing
            doc_token = _verify_order_lines(doc=doc, group=group, request_hash=request_hash)
            for allocation in doc_token:
                all_allocations.append(allocation)
            continue

        created_doc = client.post(PURCHASE_ORDER_ENTITY, header_payload)
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

        doc_token = _verify_order_lines(doc=doc, group=group, request_hash=request_hash)
        for allocation in doc_token:
            all_allocations.append(allocation)
        created += 1

    return created, all_allocations, {"status": "ok", "orders": created}


def _resolve_default_materializer() -> MaterializerCallable:
    config = _load_odata_config()
    if not isinstance(config, dict) or not str(config.get("base_url") or "").strip():
        return _materializer_not_configured
    return _materialize_purchase_control_orders_to_1c


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
    key = _make_idempotency_key(snapshot, row_keys, selected_rows)
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

    _acquire_materialization_lock(db, key)
    existing = db.query(models.PurchaseExportBatch).filter_by(idempotency_key=key).one_or_none()
    if existing is not None:
        if existing.status == "completed":
            return {
                **preview,
                "batch_id": int(existing.id),
                "status": existing.status,
                "result": existing.result_payload,
                "created_at": existing.created_at.isoformat() if existing.created_at else None,
                "completed_at": existing.completed_at.isoformat()
                if existing.completed_at
                else None,
            }
        if existing.status in {"building", "failed", "aborted"}:
            raise PurchaseControlMaterializationError(
                "another materialization batch with same lineage is not in terminal state"
            )

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

    writer = materializer if materializer is not None else _resolve_default_materializer()

    try:
        created_qty, allocations_raw, writer_result = writer(
            db,
            groups,
            request_payload,
            int(batch.id),
            dry_run,
        )
        if not isinstance(created_qty, int) or created_qty < 0:
            raise PurchaseControlMaterializationError("materializer returned malformed created_qty")

        if not isinstance(allocations_raw, list):
            raise PurchaseControlMaterializationError("materializer returned malformed allocations")

        expected: dict[int, float] = {}
        for group in groups:
            for line in group.lines:
                reservation_id = int(line.reservation_id)
                expected[reservation_id] = round(
                    float(expected.get(reservation_id, 0.0)) + float(round(line.qty, 6)),
                    6,
                )

        allocation_payload = _build_allocation_records(
            [dict(a) for a in allocations_raw],
            expected,
            int(batch.id),
        )
        for record in allocation_payload["records"]:
            db.add(record)

        allocation_result = _build_writer_result(
            expected=expected,
            seen=allocation_payload["seen"],
            allocations_by_order=allocation_payload["by_order_ref"],
            result_payload=writer_result,
            created_qty=created_qty,
        )

        batch.status = "completed"
        batch.completed_at = datetime.now(timezone.utc)
        batch.result_payload = allocation_result
        db.commit()

        return {
            **preview,
            "batch_id": int(batch.id),
            "status": batch.status,
            "result": batch.result_payload,
        }

    except Exception as exc:  # noqa: BLE001
        batch.status = "failed"
        batch.reason = str(exc)
        batch.completed_at = datetime.now(timezone.utc)
        batch.result_payload = {
            "status": "failed",
            "reason": str(exc),
        }
        db.add(batch)
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
