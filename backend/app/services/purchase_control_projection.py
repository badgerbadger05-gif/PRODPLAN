"""Immutable Ledger-native read boundary for the purchase control journal."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import math
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.reservation import (
    replenishment_execution_pct,
    replenishment_remaining,
    reservation_business_identity,
)
from app.services.planning_truth import (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_PURCHASE_CONTROL_JOURNAL,
    CAPABILITY_RESERVATION_REPLAY,
    PlanningTruthUnavailable,
    get_truth_state,
)
from app.services.odata_config import load_odata_config as _load_odata_config
from app.services.production_control_common import to_float_strict as _to_float
from app.services.supplier_order_status import phase_value, state_counts_in_mrp
from app.services.item_ledger.future_supply_read import future_supply_model


CONSUMER = "purchase_control_journal"
SNAPSHOT_KEY = "journal:v1"
REQUIRED = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_PURCHASE_CONTROL_JOURNAL,
)

_BUY_MODE = "buy"
_BUY_ROW_PREFIX = "buy:"
_BUY_ROW_GENERATOR = "mrp_reservation"
_EPS_FLOAT = 1e-9

_RU_MONTHS = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}


class PurchaseJournalUnavailable(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(detail["reason"])

    def as_dict(self):
        return dict(self.detail)


class PurchaseControlCompactPayloadError(ValueError):
    """Bounded compact purchase payload is unavailable or ambiguous."""


def _unavailable(db: Session, reason: str, truth: dict[str, Any] | None = None):
    state = get_truth_state(db)
    detail = {
        "code": "purchase_control_current_unavailable",
        "consumer": CONSUMER,
        "status": "unavailable",
        "truth_status": state.status,
        "ledger_generation": state.generation_id,
        "cutoff": state.cutoff.isoformat() if state.cutoff else None,
        "reason": reason,
    }
    if truth:
        detail["truth"] = jsonable_encoder(truth)
    return PurchaseJournalUnavailable(detail)


def validate_purchase_control_journal_buy_row(row: Any) -> None:
    if not isinstance(row, dict):
        raise ValueError("purchase control buy row is malformed")
    if str(row.get("row_generator") or "") != _BUY_ROW_GENERATOR:
        raise ValueError("purchase control buy row has unsupported generator")
    if not str(row.get("row_key") or "").startswith(_BUY_ROW_PREFIX):
        raise ValueError("purchase control buy row key is malformed")
    if "received_qty" not in row:
        raise ValueError("purchase control buy row received_qty is required")

    required_qty = _to_float(row["required_qty"])
    realized_qty = _to_float(row["realized_qty"])
    open_order_covered_qty = _to_float(row["open_order_covered_qty"])
    to_order_qty = _to_float(row["to_order_qty"])
    quantity = _to_float(row["quantity"])
    remaining_qty = _to_float(row["remaining_qty"])
    received_qty = _to_float(row["received_qty"])
    if required_qty < 0 or realized_qty < 0 or open_order_covered_qty < 0:
        raise ValueError("purchase control buy row has invalid quantities")
    if to_order_qty < 0:
        raise ValueError("purchase control buy row has invalid quantities")
    if quantity < 0 or remaining_qty < 0:
        raise ValueError("purchase control buy row has invalid quantities")
    if received_qty < 0 or received_qty > required_qty + _EPS_FLOAT:
        raise ValueError("purchase control buy row has invalid quantities")
    if not math.isclose(quantity, required_qty, abs_tol=_EPS_FLOAT):
        raise ValueError("purchase control buy row quantity is inconsistent")
    if not math.isclose(received_qty, realized_qty, abs_tol=_EPS_FLOAT):
        raise ValueError("purchase control buy row quantity is inconsistent")
    if not math.isclose(remaining_qty, to_order_qty, abs_tol=_EPS_FLOAT):
        raise ValueError("purchase control buy row quantity is inconsistent")
    if not math.isclose(
        realized_qty + open_order_covered_qty + to_order_qty,
        required_qty,
        abs_tol=_EPS_FLOAT,
    ):
        raise ValueError("purchase control buy row quantity is inconsistent")
    price = row.get("price")
    amount = row.get("amount")
    if price is None:
        if amount is not None:
            raise ValueError("purchase control buy row amount requires price")
    else:
        numeric_price = _to_float(price)
        numeric_amount = _to_float(amount)
        if numeric_price < 0 or numeric_amount < 0:
            raise ValueError("purchase control buy row has invalid accounting price")
        if not math.isclose(
            numeric_amount,
            round(to_order_qty * numeric_price, 2),
            abs_tol=0.005,
        ):
            raise ValueError("purchase control buy row amount is inconsistent")
    reservation_ids = row.get("reservation_ids")
    requirement_ids = row.get("requirement_ids")
    if (
        not isinstance(reservation_ids, list)
        or not reservation_ids
        or not isinstance(requirement_ids, list)
        or not requirement_ids
    ):
        raise ValueError("purchase control buy row lineage is malformed")
    run_id = row.get("run_id")
    run_ids = row.get("run_ids")
    if run_id is not None:
        if not isinstance(run_ids, list):
            raise ValueError("purchase control buy row lineage is malformed")
        if int(run_id) not in [int(v) for v in run_ids]:
            raise ValueError("purchase control buy row lineage is malformed")
    elif run_ids is not None:
        if not isinstance(row.get("run_ids"), list):
            raise ValueError("purchase control buy row lineage is malformed")
        if not run_ids:
            raise ValueError("purchase control buy row lineage is malformed")


def validate_purchase_control_journal_supply_row(row: Any) -> None:
    if not isinstance(row, dict):
        raise ValueError("purchase control supplier row is malformed")
    if str(row.get("row_generator") or "") != "ledger_future_supply":
        raise ValueError("purchase control supplier row has unsupported generator")
    if not str(row.get("row_key") or "").startswith("ledger-supply:"):
        raise ValueError("purchase control supplier row key is malformed")
    if row.get("fact_source") != "ledger" or row.get("fact_status") != "available":
        raise ValueError("purchase control supplier row violates the Ledger fact contract")
    ordered = _to_float(row.get("quantity"))
    realized = _to_float(row.get("received_qty"))
    open_qty = _to_float(row.get("remaining_qty"))
    if ordered < 0 or realized < 0 or open_qty < 0 or open_qty > ordered + _EPS_FLOAT:
        raise ValueError("purchase control supplier row has invalid quantities")
    if not str(row.get("order_ref1c") or "").strip():
        raise ValueError("purchase control supplier row identity is malformed")
    if not str(row.get("item_code") or "").strip():
        raise ValueError("purchase control supplier row item is malformed")


def validate_purchase_control_journal_row(row: Any) -> None:
    if isinstance(row, dict) and row.get("row_generator") == _BUY_ROW_GENERATOR:
        validate_purchase_control_journal_buy_row(row)
        return
    validate_purchase_control_journal_supply_row(row)


def _period_label(period_to: Any) -> str | None:
    if period_to is None:
        return None
    return f"{_RU_MONTHS[int(period_to.month)]} {int(period_to.year)}"


def _clean_ref(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _supplier_current_row_key(
    current_identity: Any,
    *,
    supply_kind: Any = "supplier_order",
    source_ref: Any = "",
    source_line_ref: Any = "",
    source_local_id: Any = "",
) -> str:
    """Encode the stable Ledger supply identity into the journal key.

    The ORM contract caps ``current_identity`` at 256 characters while the
    journal keeps its historical ``ledger-supply:`` namespace.  Preserve the
    identity verbatim for normal captures and use a deterministic digest only
    for the bounded edge case.
    """
    identity = _clean_ref(current_identity)
    # Compatibility for pre-R7 fixtures/captures that predate the populated
    # column.  This is the same exact-source identity used by capture; it is
    # stable across physical generation rows and never uses ``supply.id``.
    if not identity and _clean_ref(source_ref) and _clean_ref(source_line_ref):
        identity = ":".join(
            (
                _clean_ref(supply_kind),
                _clean_ref(source_ref),
                _clean_ref(source_line_ref),
                _clean_ref(source_local_id),
            )
        )
    if not identity:
        raise ValueError("LedgerFutureSupply supplier-order current identity is missing")
    prefix = "ledger-supply:"
    if len(identity) <= 256 - len(prefix):
        return f"{prefix}{identity}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{prefix}sha256:{digest}"


def _overdue_days(need_date_iso: Any, cutoff_date: date | None) -> int:
    """Days the demand date is already in the past at the truth cutoff.

    The journal is an immutable projection pinned to a Ledger cutoff, so the
    only honest "today" is that cutoff. Wall-clock time would make the frozen
    snapshot answer differently on every read.
    """
    if cutoff_date is None or not need_date_iso:
        return 0
    try:
        need = date.fromisoformat(str(need_date_iso))
    except ValueError:
        return 0
    return max((cutoff_date - need).days, 0)


def _supplier_line_status(
    *,
    open_qty: float,
    realized_qty: float,
    eta_date: date | None,
    cutoff_date: date,
    supply_phase: str,
) -> str:
    """Project a display status from generation-pinned supplier evidence."""
    if supply_phase == "terminal":
        return "closed"
    if open_qty <= _EPS_FLOAT:
        return "received"
    if eta_date is not None and eta_date < cutoff_date:
        return "overdue"
    if realized_qty > _EPS_FLOAT:
        return "partial"
    if eta_date is None:
        return "no_date"
    return "expected"


def order_block_reason(
    *,
    to_order_qty: float,
    supplier_id: Any,
    item_ref1c: Any,
    unit: Any,
    planning_stock_pool: Any,
) -> str | None:
    """Why this demand row cannot be turned into a supplier order, if it cannot.

    The read boundary owns action availability (see `.docs/api.md`,
    "Общий контракт ответа": готовые флаги разрешённых действий). The checks
    mirror the preconditions `purchase_control_materialization` enforces, so a
    row flagged orderable does not explode on export.
    """
    if to_order_qty <= _EPS_FLOAT:
        return "нет остатка потребности к заказу"
    if supplier_id is None:
        return "у номенклатуры не указан поставщик"
    if not str(item_ref1c or "").strip():
        return "у номенклатуры нет ссылки 1С"
    if not str(unit or "").strip():
        return "у номенклатуры не задана единица измерения"
    if not str(planning_stock_pool or "").strip():
        return "не определён пул планирования"
    return None


def _run_horizons(db: Session, run_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not run_ids:
        return {}
    rows = (
        db.query(models.PlanningRun.run_id, models.PlanningRun.period_from, models.PlanningRun.period_to)
        .filter(models.PlanningRun.run_id.in_(sorted(run_ids)))
        .all()
    )
    return {
        int(row[0]): {
            "from": row[1],
            "to": row[2],
        }
        for row in rows
    }


def open_supplier_coverage_by_reservation(
    db: Session,
    generation_id: int,
    entries: list[tuple[Any, Any, Any]],
    *,
    affected_scopes: Sequence[tuple[int, str, str, str, str]] | None = None,
) -> tuple[dict[int, float], dict[int, list[dict[str, Any]]]]:
    """Allocate frozen supplier-order remainder to active BUY reservations.

    ``MrpFreezeAllocation`` is the immutable obligation-to-order lineage.
    ``LedgerFutureSupply`` is the generation-pinned fact for what remains open
    on that order line at the cutoff.  Joining those saved sources keeps open
    orders out of physical fulfillment while preventing them from being
    proposed for purchase a second time.
    """
    if not entries:
        return {}, {}

    reservation_by_requirement: dict[int, models.ReservationEntry] = {}
    outstanding_by_reservation: dict[int, float] = {}
    for work_item, reservation, _item in entries:
        requirement_id = int(work_item.requirement_id)
        current = reservation_by_requirement.get(requirement_id)
        if current is not None and int(current.id) != int(reservation.id):
            raise ValueError("buy requirement maps to multiple active reservations")
        reservation_by_requirement[requirement_id] = reservation
        outstanding_by_reservation[int(reservation.id)] = _to_float(
            work_item.replenishment_remaining_qty
        )

    supplies: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    future_supply = future_supply_model(db, int(generation_id))
    supply_query = db.query(future_supply).filter(
        future_supply.ledger_generation_id == int(generation_id),
        future_supply.supply_kind == "supplier_order",
        future_supply.evidence_status == "exact",
        future_supply.open_qty_at_cutoff > _EPS_FLOAT,
    )
    if affected_scopes is not None:
        item_pool_pairs = sorted({
            (int(scope[0]), _clean_ref(scope[3]))
            for scope in affected_scopes
        })
        if item_pool_pairs:
            supply_query = supply_query.filter(or_(*(
                and_(
                    future_supply.item_id == item_id,
                    future_supply.planning_stock_pool == pool,
                )
                for item_id, pool in item_pool_pairs
            )))
        else:
            supply_query = supply_query.filter(future_supply.id < 0)
    for supply in (
        supply_query
        .order_by(
            future_supply.eta_date.asc(),
            future_supply.id.asc(),
        )
        .all()
    ):
        source_ref = _clean_ref(supply.source_ref)
        source_line_ref = _clean_ref(supply.source_line_ref)
        pool = _clean_ref(supply.planning_stock_pool)
        if not source_ref or not source_line_ref or not pool:
            raise ValueError("exact supplier future supply lacks stable identity")
        if supply.eta_date is None:
            raise ValueError("exact supplier future supply lacks ETA")
        key = (int(supply.item_id), pool, source_ref, source_line_ref)
        if key in supplies:
            raise ValueError("supplier future supply identity is duplicated")
        supplies[key] = {
            "initial": _to_float(supply.open_qty_at_cutoff),
            "remaining": _to_float(supply.open_qty_at_cutoff),
            "eta": supply.eta_date,
            "id": int(supply.id),
        }

    if not supplies:
        return {}, {}

    requirement_ids = sorted(reservation_by_requirement)
    claims_query = db.query(models.MrpFreezeAllocation).filter(
        models.MrpFreezeAllocation.requirement_id.in_(requirement_ids),
        models.MrpFreezeAllocation.source_type == "supplier_order",
    )
    claims = claims_query.all()
    claims.sort(
        key=lambda allocation: (
            reservation_by_requirement[int(allocation.requirement_id)].priority_period_from,
            reservation_by_requirement[int(allocation.requirement_id)].priority_period_to,
            int(allocation.run_id),
            int(allocation.id),
        )
    )

    covered_by_reservation: dict[int, float] = {}
    slices_by_reservation: dict[int, list[dict[str, Any]]] = {}
    for allocation in claims:
        reservation = reservation_by_requirement.get(int(allocation.requirement_id))
        if reservation is None:
            continue
        if (
            int(allocation.run_id) != int(reservation.run_id or -1)
            or int(allocation.freeze_version) != int(reservation.freeze_version)
        ):
            continue
        reservation_id = int(reservation.id)
        reservation_left = max(
            outstanding_by_reservation.get(reservation_id, 0.0)
            - covered_by_reservation.get(reservation_id, 0.0),
            0.0,
        )
        if reservation_left <= _EPS_FLOAT:
            continue

        key = (
            int(allocation.item_id),
            _clean_ref(allocation.planning_stock_pool),
            _clean_ref(allocation.source_ref),
            _clean_ref(allocation.source_line_ref),
        )
        supply = supplies.get(key)
        line_left = _to_float(supply["remaining"]) if supply is not None else 0.0
        if line_left <= _EPS_FLOAT:
            continue
        take = min(_to_float(allocation.alloc_qty), line_left, reservation_left)
        if take <= _EPS_FLOAT:
            continue

        supply["remaining"] = max(line_left - take, 0.0)
        covered_by_reservation[reservation_id] = (
            covered_by_reservation.get(reservation_id, 0.0) + take
        )
        slices_by_reservation.setdefault(reservation_id, []).append(
            {
                "source_type": "supplier_order",
                "source_ref": key[2],
                "source_line_ref": key[3],
                "covered_qty": round(take, 6),
            }
        )

    # A supplier order created directly in 1C has no immutable
    # MrpFreezeAllocation because it did not exist when the plan was frozen.
    # Allocate every still-unclaimed exact line by the canonical obligation
    # order.  The persisted generation snapshot makes this deterministic and
    # immutable; item/pool equality prevents cross-contour coverage.
    reservations = sorted(
        (
            reservation
            for reservation in reservation_by_requirement.values()
            if outstanding_by_reservation.get(int(reservation.id), 0.0)
            > _EPS_FLOAT
        ),
        key=lambda reservation: (
            reservation.priority_period_from,
            reservation.priority_period_to,
            int(reservation.run_id),
            int(reservation.id),
        ),
    )
    supply_keys = sorted(
        supplies,
        key=lambda key: (
            supplies[key]["eta"] or date.min,
            key[2],
            key[3],
            supplies[key]["id"],
        ),
    )
    for key in supply_keys:
        supply = supplies[key]
        line_left = _to_float(supply["remaining"])
        if line_left <= _EPS_FLOAT:
            continue
        for reservation in reservations:
            reservation_id = int(reservation.id)
            if (
                int(reservation.item_id) != key[0]
                or _clean_ref(reservation.planning_stock_pool) != key[1]
            ):
                continue
            reservation_left = max(
                outstanding_by_reservation.get(reservation_id, 0.0)
                - covered_by_reservation.get(reservation_id, 0.0),
                0.0,
            )
            if reservation_left <= _EPS_FLOAT:
                continue
            take = min(line_left, reservation_left)
            if take <= _EPS_FLOAT:
                continue
            line_left = max(line_left - take, 0.0)
            supply["remaining"] = line_left
            covered_by_reservation[reservation_id] = (
                covered_by_reservation.get(reservation_id, 0.0) + take
            )
            slices_by_reservation.setdefault(reservation_id, []).append(
                {
                    "source_type": "supplier_order",
                    "source_ref": key[2],
                    "source_line_ref": key[3],
                    "covered_qty": round(take, 6),
                }
            )
            if line_left <= _EPS_FLOAT:
                break

    for reservation_id, covered in covered_by_reservation.items():
        if covered > outstanding_by_reservation.get(reservation_id, 0.0) + _EPS_FLOAT:
            raise ValueError("supplier future supply exceeds reservation remainder")
    for supply in supplies.values():
        if (
            _to_float(supply["remaining"]) < -_EPS_FLOAT
            or _to_float(supply["remaining"]) > _to_float(supply["initial"]) + _EPS_FLOAT
        ):
            raise ValueError("supplier future supply allocation violates conservation")

    return covered_by_reservation, slices_by_reservation


def _build_supplier_card_rows(
    db: Session,
    generation: models.LedgerGeneration,
    *,
    affected_scopes: Sequence[tuple[int, str, str, str, str]] | None = None,
    cutoff_date: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    effective_cutoff_date = cutoff_date or generation.cutoff.date()
    future_supply = future_supply_model(db, int(generation.id))
    supply_query = (
        db.query(future_supply, models.Item)
        .join(models.Item, models.Item.item_id == future_supply.item_id)
        .filter(
            future_supply.ledger_generation_id == generation.id,
            future_supply.supply_kind == "supplier_order",
        )
    )
    if affected_scopes is not None:
        supply_query = supply_query.filter(or_(*(
            and_(
                future_supply.item_id == int(scope[0]),
                future_supply.characteristic_ref == _clean_ref(scope[1]),
                future_supply.organization_ref == _clean_ref(scope[2]),
                future_supply.planning_stock_pool == _clean_ref(scope[3]),
            )
            for scope in affected_scopes
        ))) if affected_scopes else supply_query.filter(future_supply.id < 0)
    supplies = supply_query.all()

    cards: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    seen_source_lines: set[tuple[str, str]] = set()

    for supply, item in supplies:
        source_ref = _clean_ref(supply.source_ref)
        source_line_ref = _clean_ref(supply.source_line_ref)
        if source_ref == "" or source_line_ref == "":
            continue

        source_identity = (source_ref, source_line_ref)
        if source_identity in seen_source_lines:
            raise ValueError("LedgerFutureSupply supplier-order source line is duplicated")
        seen_source_lines.add(source_identity)

        if supply.evidence_status != "exact":
            continue

        try:
            ordered = _to_float(supply.ordered_qty_at_cutoff)
            realized = _to_float(supply.realized_qty_at_cutoff)
            open_qty = _to_float(supply.open_qty_at_cutoff)
        except ValueError as exc:
            raise ValueError("LedgerFutureSupply supplier-order quantities are missing or invalid") from exc

        if ordered < 0 or realized < 0 or open_qty < 0 or open_qty > ordered:
            raise ValueError("LedgerFutureSupply supplier-order quantities violate ordered/open invariant")
        if not str(item.item_code or "").strip():
            raise ValueError("LedgerFutureSupply supplier-order item has no code")

        order = db.query(models.SupplierOrder).filter(models.SupplierOrder.order_ref1c == source_ref).one_or_none()
        supplier = (
            db.get(models.Supplier, order.supplier_id)
            if order is not None and order.supplier_id is not None
            else None
        )

        order_state_name = str(supply.source_state_key or "")
        supply_phase = phase_value(order_state_name)
        overdue_days = (
            max((effective_cutoff_date - supply.eta_date).days, 0)
            if supply.eta_date is not None
            else 0
        )
        row = {
            "row_key": _supplier_current_row_key(
                supply.current_identity,
                supply_kind=supply.supply_kind,
                source_ref=source_ref,
                source_line_ref=source_line_ref,
                source_local_id=supply.source_local_id,
            ),
            "line_id": None,
            "purchase_id": None,
            "source_purchase_ids": [],
            "order_id": int(order.order_id) if order else None,
            "order_number": str(order.order_number or "") if order else source_ref,
            "order_date": order.order_date.isoformat() if order and order.order_date else None,
            "order_ref1c": supply.source_ref,
            "order_state_name": order_state_name,
            "supply_phase": supply_phase,
            "counts_in_mrp": state_counts_in_mrp(order_state_name),
            "source": "ledger",
            "supplier_id": int(order.supplier_id) if order and order.supplier_id is not None else None,
            "supplier_name": str(supplier.supplier_name or "") if supplier else "",
            "item_id": int(item.item_id),
            "item_code": str(item.item_code or ""),
            "item_article": item.item_article,
            "item_name": str(item.item_name or ""),
            "unit": item.unit,
            "planning_stock_pool": _clean_ref(supply.planning_stock_pool),
            "quantity": ordered,
            "received_qty": realized,
            "remaining_qty": open_qty,
            "delivery_date": supply.eta_date.isoformat() if supply.eta_date else None,
            "need_date": None,
            "overdue_days": overdue_days,
            "line_status": _supplier_line_status(
                open_qty=open_qty,
                realized_qty=realized,
                eta_date=supply.eta_date,
                cutoff_date=effective_cutoff_date,
                supply_phase=supply_phase,
            ),
            "price": None,
            "amount": None,
            "run_id": None,
            "run_ids": [],
            "row_generator": "ledger_future_supply",
            "fact_status": "available",
            "fact_source": "ledger",
        }
        rows.append(row)

        if order is not None:
            header = {
                k: row.get(k)
                for k in (
                    "order_id",
                    "order_number",
                    "order_date",
                    "order_ref1c",
                    "order_state_name",
                    "supply_phase",
                    "counts_in_mrp",
                    "supplier_id",
                    "supplier_name",
                )
            }
            header.update(
                {
                    "deletion_mark": bool(order.deletion_mark),
                    "is_posted": bool(order.is_posted),
                    "document_amount": float(order.document_amount or 0),
                    "active": not bool(order.deletion_mark),
                    "source": "1c",
                }
            )
            card = cards.setdefault(str(int(order.order_id)), {"order": header, "lines": []})
            if card["order"] != header:
                raise ValueError("conflicting frozen supplier-order header")
            card["lines"].append(row)

    for card in cards.values():
        card["lines"].sort(key=lambda row: (str(row["item_code"]), str(row["row_key"])))
    return rows, cards


def _build_buyer_rows(
    db: Session,
    generation_id: int,
    to_order_by_period: list[dict[str, Any]],
    cutoff_date: date | None = None,
    *,
    entries_override: Sequence[tuple[Any, Any, Any]] | None = None,
    affected_scopes: Sequence[tuple[int, str, str, str, str]] | None = None,
) -> list[dict[str, Any]]:
    configured_destination_warehouse_ref1c = _clean_ref(
        _load_odata_config().get("purchase_destination_warehouse_ref1c")
    )

    if entries_override is None:
        entries = (
            db.query(
                models.ReplenishmentWorkItem,
                models.ReservationEntry,
                models.Item,
            )
            .join(
                models.ReservationEntry,
                models.ReservationEntry.id
                == models.ReplenishmentWorkItem.reservation_id,
            )
            .join(
                models.Item,
                models.Item.item_id == models.ReplenishmentWorkItem.item_id,
            )
            .filter(
                models.ReplenishmentWorkItem.ledger_generation_id == generation_id,
                models.ReplenishmentWorkItem.replenishment_method == _BUY_MODE,
                models.ReservationEntry.lifecycle_status == "active",
            )
            .order_by(
                models.Item.item_code.asc(),
                models.ReservationEntry.planning_stock_pool.asc(),
                models.ReplenishmentWorkItem.run_id.asc(),
                models.ReplenishmentWorkItem.id.asc(),
            )
            .all()
        )
    else:
        entries = list(entries_override)
    if not entries:
        return []

    open_covered_by_reservation, open_coverage_slices = (
        open_supplier_coverage_by_reservation(
            db, int(generation_id), entries, affected_scopes=affected_scopes
        )
    )

    supplier_refs = {
        _clean_ref(s.supplier_ref1c).lower(): int(s.supplier_id)
        for s in db.query(models.Supplier).all()
        if _clean_ref(s.supplier_ref1c)
    }
    supplier_names = {
        _clean_ref(s.supplier_ref1c).lower(): str(s.supplier_name or "")
        for s in db.query(models.Supplier).all()
        if _clean_ref(s.supplier_ref1c)
    }
    supplier_by_id = {
        int(s.supplier_id): str(s.supplier_name or "")
        for s in db.query(models.Supplier).all()
        if s.supplier_id is not None
    }

    grouped_rows: dict[tuple[int, str], dict[str, Any]] = {}

    run_ids: set[int] = {
        int(work_item.run_id)
        for work_item, _reservation, _item in entries
    }
    horizons = _run_horizons(db, run_ids)

    for work_item, reservation, item in entries:
        run_id = int(work_item.run_id)
        horizon = horizons.get(run_id)
        if horizon is None:
            raise ValueError("buy reservation references planning run without period horizon")
        period_from = horizon.get("from")
        period_to = horizon.get("to")
        if period_from is None or period_to is None:
            raise ValueError("buy reservation run has incomplete plan horizon")

        required = _to_float(work_item.replenishment_required_qty)
        realized = _to_float(work_item.replenishment_fulfilled_qty)
        remaining_after_receipts = _to_float(work_item.replenishment_remaining_qty)
        open_order_covered = min(
            open_covered_by_reservation.get(int(reservation.id), 0.0),
            remaining_after_receipts,
        )
        to_order = max(remaining_after_receipts - open_order_covered, 0.0)

        if required < 0 or realized < 0 or remaining_after_receipts < 0:
            raise ValueError("buy reservation has invalid quantities")
        if realized > required + _EPS_FLOAT:
            raise ValueError("buy reservation realized exceeds reserved")
        if not math.isclose(
            remaining_after_receipts,
            float(replenishment_remaining(required, realized)),
            abs_tol=_EPS_FLOAT,
        ):
            raise ValueError("buy reservation has inconsistent uncovered quantity")

        pool = _clean_ref(reservation.planning_stock_pool) or "main"
        item_code = str(item.item_code or "")
        key = (int(item.item_id), pool)
        target = grouped_rows.setdefault(
            key,
            {
                "item_id": int(item.item_id),
                "item_code": item_code,
                "item_article": item.item_article,
                "item_name": str(item.item_name or ""),
                "unit": item.unit,
                "planning_stock_pool": pool,
                "item_ref1c": _clean_ref(item.item_ref1c),
                "accounting_price": (
                    float(item.accounting_price)
                    if item.accounting_price is not None
                    else None
                ),
                "supplier_ref1c": _clean_ref(item.supplier_ref1c).lower(),
                "requirement_ids": set(),
                "reservation_ids": set(),
                "run_ids": set(),
                "required_qty": 0.0,
                "realized_qty": 0.0,
                "open_order_covered_qty": 0.0,
                "to_order_qty": 0.0,
                "slices": [],
                "horizon_buckets": [],
            },
        )

        requirement_id = int(work_item.requirement_id)

        target["requirement_ids"].add(requirement_id)
        target["reservation_ids"].add(int(reservation.id))
        target["run_ids"].add(run_id)
        target["required_qty"] += required
        target["realized_qty"] += realized
        target["open_order_covered_qty"] += open_order_covered
        target["to_order_qty"] += to_order

        # The demand date belongs to the requirement, not to the plan window:
        # the horizon cut must trim by "когда нужно", not by "когда кончается
        # план". Plan bounds stay as the fallback for legacy reservations.
        need_from = reservation.priority_period_from or period_from
        need_to = reservation.priority_period_to or period_to

        to_order_pct = replenishment_execution_pct(required, to_order)
        open_order_covered_pct = replenishment_execution_pct(
            required,
            open_order_covered,
        )

        target["slices"].append(
            {
                "reservation_id": int(reservation.id),
                "work_item_id": int(work_item.id),
                "requirement_id": requirement_id,
                "run_id": run_id,
                "plan_period_from": period_from.isoformat() if period_from else None,
                "plan_period_to": period_to.isoformat() if period_to else None,
                "need_date": need_from.isoformat() if need_from else None,
                "need_period_to": need_to.isoformat() if need_to else None,
                "period_label": _period_label(period_to),
                "required_qty": required,
                "realized_qty": realized,
                "open_order_covered_qty": open_order_covered,
                "to_order_qty": to_order,
                "to_order_pct": float(to_order_pct) if to_order_pct is not None else None,
                "open_order_covered_pct": (
                    float(open_order_covered_pct)
                    if open_order_covered_pct is not None
                    else None
                ),
                "coverage_slices": list(
                    open_coverage_slices.get(int(reservation.id), [])
                ),
            }
        )
        target["horizon_buckets"].append(target["slices"][-1])

    rows: list[dict[str, Any]] = []
    for _key, payload in grouped_rows.items():
        required_qty = round(float(payload["required_qty"]), 3)
        realized_qty = round(float(payload["realized_qty"]), 3)
        open_order_covered_qty = round(float(payload["open_order_covered_qty"]), 3)
        to_order_qty = round(float(payload["to_order_qty"]), 3)
        if required_qty <= 0 and to_order_qty <= 0:
            continue

        sorted_runs = sorted(int(v) for v in payload["run_ids"])
        first_bucket = min(
            payload["horizon_buckets"],
            key=lambda row: str(row["plan_period_to"] or ""),
        )
        last_bucket = max(
            payload["horizon_buckets"],
            key=lambda row: str(row["plan_period_to"] or ""),
        )

        coverage_by_period: dict[str, dict[str, Any]] = {}
        for bucket in payload["horizon_buckets"]:
            pto = bucket["plan_period_to"]
            key = str(pto or "")
            holder = coverage_by_period.setdefault(
                key,
                {
                    "plan_period_to": pto,
                    "period_label": bucket["period_label"],
                    "run_id": int(bucket["run_id"]),
                    "required_qty": 0.0,
                    "to_order_qty": 0.0,
                    "open_order_covered_qty": 0.0,
                },
            )
            holder["required_qty"] += float(bucket["required_qty"])
            holder["to_order_qty"] += float(bucket["to_order_qty"])
            holder["open_order_covered_qty"] += float(bucket["open_order_covered_qty"])

        supplier_ref = payload["supplier_ref1c"]
        supplier_id = supplier_refs.get(supplier_ref)
        supplier_name = supplier_names.get(supplier_ref)
        if supplier_name is None and supplier_id is not None:
            supplier_name = supplier_by_id.get(supplier_id, "")

        for b in coverage_by_period.values():
            to_order_by_period.append(
                {
                    "plan_period_to": b["plan_period_to"],
                    "period_label": b["period_label"],
                    "item_count": 1,
                    "total_qty": round(float(b["to_order_qty"]), 3),
                }
            )

        to_order_pct = replenishment_execution_pct(required_qty, to_order_qty)
        open_order_covered_pct = replenishment_execution_pct(
            required_qty,
            open_order_covered_qty,
        )

        row = {
            "row_key": f"buy:{payload['item_id']}:{payload['planning_stock_pool']}",
            "line_id": None,
            "purchase_id": None,
            "source_purchase_ids": [],
            "order_id": None,
            "order_number": "",
            "order_date": None,
            "order_ref1c": None,
            "order_state_name": None,
            "supply_phase": "no_goods",
            "counts_in_mrp": None,
            "source": "mrp",
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "item_id": payload["item_id"],
            "item_code": payload["item_code"],
            "item_article": payload["item_article"],
            "item_name": payload["item_name"],
            "unit": payload["unit"],
            "quantity": required_qty,
            "received_qty": realized_qty,
            "remaining_qty": to_order_qty,
            "delivery_date": last_bucket["plan_period_to"],
            "need_date": first_bucket["plan_period_from"],
            "overdue_days": 0,
            "line_status": (
                "to_order"
                if to_order_qty > _EPS_FLOAT
                else "expected"
                if open_order_covered_qty > _EPS_FLOAT
                else "received"
            ),
            "price": payload["accounting_price"],
            "amount": (
                round(to_order_qty * float(payload["accounting_price"]), 2)
                if payload["accounting_price"] is not None
                else None
            ),
            "run_id": sorted_runs[0] if len(sorted_runs) == 1 else None,
            "run_ids": sorted_runs,
            "requirement_ids": sorted(payload["requirement_ids"]),
            "reservation_ids": sorted(payload["reservation_ids"]),
            "planning_stock_pool": payload["planning_stock_pool"],
            "required_qty": required_qty,
            "realized_qty": realized_qty,
            "open_order_covered_qty": open_order_covered_qty,
            "to_order_qty": to_order_qty,
            "to_order_pct": float(to_order_pct) if to_order_pct is not None else None,
            "open_order_covered_pct": (
                float(open_order_covered_pct)
                if open_order_covered_pct is not None
                else None
            ),
            "plan_period_from": first_bucket["plan_period_from"],
            "plan_period_to": last_bucket["plan_period_to"],
            "period_label": first_bucket["period_label"],
            "horizon_bucket_count": len(payload["horizon_buckets"]),
            "horizon_buckets": payload["horizon_buckets"],
            "slices": payload["slices"],
            "materialization_input": {
                "version": 1,
                "supplier_ref1c": payload["supplier_ref1c"],
                "item_ref1c": payload["item_ref1c"],
                "unit_ref1c": _clean_ref(payload["unit"]),
                "destination_warehouse_ref1c": configured_destination_warehouse_ref1c,
                "slices": [
                    {
                        "reservation_id": int(slice_row["reservation_id"]),
                        "work_item_id": int(slice_row["work_item_id"]),
                        "requirement_id": int(slice_row["requirement_id"]),
                        "run_id": int(slice_row["run_id"]),
                        "plan_period_from": slice_row["plan_period_from"],
                        "plan_period_to": slice_row["plan_period_to"],
                        "need_date": slice_row["need_date"],
                        "need_period_to": slice_row["need_period_to"],
                        "to_order_qty": round(float(slice_row["to_order_qty"]), 6),
                    }
                    for slice_row in payload["slices"]
                ],
            },
            "row_generator": _BUY_ROW_GENERATOR,
            "fact_status": "available",
            "fact_source": "ledger",
        }
        validate_purchase_control_journal_buy_row(row)
        rows.append(row)
    rows.sort(
        key=lambda row: (
            str(row["supplier_id"] or ""),
            str(row["item_code"]),
            str(row["row_key"]),
            str(row["planning_stock_pool"]),
        )
    )
    return rows


def build_candidate_payload(db: Session, generation_id: int) -> dict[str, Any]:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or generation.status != "building" or generation.cutoff is None:
        raise ValueError("purchase journal candidate requires BUILDING Ledger generation")

    to_order_buckets: list[dict[str, Any]] = []
    supplier_rows, cards = _build_supplier_card_rows(db, generation)
    buyer_rows = [
        dict(row)
        for row in _build_buyer_rows(
            db,
            int(generation.id),
            to_order_buckets,
            cutoff_date=generation.cutoff.date(),
        )
        if float(row.get("remaining_qty") or 0) >= 0
    ]
    buyer_rows = _with_stable_reservation_lineage(db, buyer_rows)
    merged_rows = [*supplier_rows, *buyer_rows]

    by_bucket: dict[str, dict[str, Any]] = {}
    for bucket in to_order_buckets:
        key = str(bucket.get("plan_period_to") or "")
        target = by_bucket.setdefault(
            key,
            {
                "plan_period_to": bucket.get("plan_period_to"),
                "period_label": bucket.get("period_label"),
                "item_count": 0,
                "total_qty": 0.0,
            },
        )
        target["item_count"] += 1
        target["total_qty"] += float(bucket.get("total_qty") or 0.0)

    to_order_by_period = [
        {
            "plan_period_to": value["plan_period_to"],
            "period_label": value["period_label"],
            "item_count": value["item_count"],
            "total_qty": round(float(value["total_qty"]), 3),
        }
        for value in by_bucket.values()
    ]
    to_order_by_period.sort(key=lambda bucket: (bucket["plan_period_to"] is None, str(bucket["plan_period_to"] or "")))

    rows = sorted(
        merged_rows,
        key=lambda row: (
            str(row.get("order_number") or ""),
            str(row.get("item_code") or ""),
            str(row.get("row_key") or ""),
            str(row.get("order_ref1c") or ""),
        ),
    )

    by_status: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    for row in rows:
        status = str(row.get("line_status") or "unavailable")
        phase = str(row.get("supply_phase") or "unavailable")
        by_status[status] = by_status.get(status, 0) + 1
        by_phase[phase] = by_phase.get(phase, 0) + 1
    persisted_summary = {
        "total_rows": len(rows),
        "by_status": by_status,
        "by_phase": by_phase,
        "to_order": by_status.get("to_order", 0),
        "overdue": by_status.get("overdue", 0),
        "expected_7d": 0,
        "in_transit_amount": 0.0,
        "fact_status": "available",
    }

    row_keys: set[str] = set()
    for row in rows:
        row_key = row.get("row_key")
        if isinstance(row_key, str):
            if row_key in row_keys:
                raise ValueError("purchase control snapshot row keys are duplicated")
            row_keys.add(row_key)

    run_ids = sorted({
        int(v)
        for row in rows
        for v in ([row.get("run_id")] if row.get("run_id") is not None else row.get("run_ids", []) or [])
    })
    supplier_states = sorted(
        {
            str(row.get("order_state_name") or "")
            for row in rows
            if row.get("order_state_name") is not None
        }
    )

    payload = {
        "meta": {
            "ledger_generation": generation.id,
            "ledger_generation_id": generation.id,
            "cutoff": generation.cutoff.isoformat(),
            "truth_status": "building",
            "read_only": True,
            "fact_source": "ledger",
            "received_qty_status": "available",
            "run_ids": run_ids,
            "to_order_by_period": to_order_by_period,
            "states": supplier_states,
        },
        "rows": rows,
        "cards": cards,
        "summary": persisted_summary,
    }

    return payload


def _normalise_compact_purchase_scopes(
    affected_scopes: Iterable[tuple[int, str, str, str, str]] | None,
) -> tuple[tuple[int, str, str, str, str], ...] | None:
    if affected_scopes is None:
        return None
    result: list[tuple[int, str, str, str, str]] = []
    seen: set[tuple[int, str, str, str, str]] = set()
    for raw in affected_scopes:
        try:
            values = tuple(raw)
            scope = (
                int(values[0]),
                _clean_ref(values[1]),
                _clean_ref(values[2]),
                _clean_ref(values[3]),
                _clean_ref(values[4]),
            )
        except (TypeError, ValueError, IndexError) as exc:
            raise PurchaseControlCompactPayloadError(
                "compact purchase affected scope is malformed"
            ) from exc
        if len(values) != 5 or scope[0] <= 0 or scope[4] != _BUY_MODE:
            raise PurchaseControlCompactPayloadError(
                "compact purchase affected scope is malformed"
            )
        if scope in seen:
            raise PurchaseControlCompactPayloadError(
                "compact purchase affected scopes contain duplicates"
            )
        seen.add(scope)
        result.append(scope)
    return tuple(sorted(result))


def _compact_purchase_row_without_work_item(
    row: dict[str, Any],
    *,
    identity_by_reservation: Mapping[int, str],
) -> dict[str, Any]:
    """Remove synthetic staged IDs while retaining stable reservation lineage."""

    compact = dict(row)
    compact["current_reservation_identities"] = sorted(
        identity_by_reservation[int(reservation_id)]
        for reservation_id in compact.get("reservation_ids", [])
        if int(reservation_id) in identity_by_reservation
    )
    for field_name in ("slices", "horizon_buckets"):
        values = []
        for raw in compact.get(field_name, []) or []:
            value = dict(raw)
            reservation_id = int(value["reservation_id"])
            value.pop("work_item_id", None)
            value["current_identity"] = identity_by_reservation[reservation_id]
            values.append(value)
        compact[field_name] = values
    materialization = compact.get("materialization_input")
    if isinstance(materialization, dict):
        materialization = dict(materialization)
        materialization["slices"] = [
            {
                key: value
                for key, value in dict(raw).items()
                if key != "work_item_id"
            }
            for raw in materialization.get("slices", []) or []
        ]
        compact["materialization_input"] = materialization
    return compact


def _with_stable_reservation_lineage(
    db: Session, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Give staged BUY rows the same stable lineage the compact path emits.

    The bounded path names each BUY row's owners by their stable current
    identity and drops generation-local work-item ids
    (``_compact_purchase_row_without_work_item``).  The obligation/accept path
    built the same row without that lineage, so every alternation between the
    two paths rewrote every BUY row (271 updates per refresh on the stand).
    One compaction for both paths.  A row whose owners carry no stable
    identity yet (pre-owner data) is left as built.
    """
    reservation_ids = {
        int(value)
        for row in rows
        for value in (row.get("reservation_ids") or [])
    } | {
        int(value["reservation_id"])
        for row in rows
        for field_name in ("slices", "horizon_buckets")
        for value in (row.get(field_name) or [])
        if isinstance(value, Mapping) and value.get("reservation_id") is not None
    }
    if not reservation_ids:
        return rows
    identity_by_reservation = {
        int(reservation_id): _clean_ref(identity)
        for reservation_id, identity in db.query(
            models.ReservationEntry.id, models.ReservationEntry.current_identity
        ).filter(models.ReservationEntry.id.in_(sorted(reservation_ids))).all()
        if _clean_ref(identity)
    }
    result: list[dict[str, Any]] = []
    for row in rows:
        needed = {int(value) for value in (row.get("reservation_ids") or [])} | {
            int(value["reservation_id"])
            for field_name in ("slices", "horizon_buckets")
            for value in (row.get(field_name) or [])
            if isinstance(value, Mapping) and value.get("reservation_id") is not None
        }
        if needed and needed <= set(identity_by_reservation):
            result.append(_compact_purchase_row_without_work_item(
                row, identity_by_reservation=identity_by_reservation
            ))
        else:
            result.append(row)
    return result


def _normalise_legacy_parent_buy_row(
    row: dict[str, Any],
    *,
    reservation_by_requirement: Mapping[int, models.ReservationEntry],
) -> dict[str, Any]:
    """Rebind a pre-R4 current BUY payload to stable reservation owners.

    Older current manifests retained staged ``reservation_id`` and
    ``work_item_id`` values.  They are provenance-only and cannot be copied
    into a new compact refresh.  Requirement lineage is the durable bridge;
    the caller supplies a complete, uniqueness-checked current-owner map.
    """
    compact = dict(row)
    raw_requirement_ids = compact.get("requirement_ids")
    if not isinstance(raw_requirement_ids, list) or not raw_requirement_ids:
        raise PurchaseControlCompactPayloadError(
            "legacy compact purchase BUY row lacks requirement lineage"
        )
    requirement_ids: list[int] = []
    for value in raw_requirement_ids:
        try:
            requirement_id = int(value)
        except (TypeError, ValueError) as exc:
            raise PurchaseControlCompactPayloadError(
                "legacy compact purchase BUY requirement identity is malformed"
            ) from exc
        if requirement_id <= 0 or requirement_id not in reservation_by_requirement:
            raise PurchaseControlCompactPayloadError(
                f"legacy compact purchase BUY owner is missing for requirement {value!r}"
            )
        requirement_ids.append(requirement_id)

    reservations = [reservation_by_requirement[requirement_id] for requirement_id in requirement_ids]
    compact["reservation_ids"] = sorted(int(reservation.id) for reservation in reservations)
    compact["current_reservation_identities"] = sorted(
        str(reservation.current_identity) for reservation in reservations
    )

    def normalise_slice(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise PurchaseControlCompactPayloadError(
                "legacy compact purchase BUY slice is malformed"
            )
        value = dict(raw)
        try:
            requirement_id = int(value["requirement_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PurchaseControlCompactPayloadError(
                "legacy compact purchase BUY slice lacks requirement lineage"
            ) from exc
        reservation = reservation_by_requirement.get(requirement_id)
        if reservation is None:
            raise PurchaseControlCompactPayloadError(
                f"legacy compact purchase BUY owner is missing for requirement {requirement_id}"
            )
        value["reservation_id"] = int(reservation.id)
        value["current_identity"] = str(reservation.current_identity)
        value.pop("work_item_id", None)
        return value

    for field_name in ("slices", "horizon_buckets"):
        raw_values = compact.get(field_name)
        if raw_values is not None:
            if not isinstance(raw_values, list):
                raise PurchaseControlCompactPayloadError(
                    f"legacy compact purchase BUY {field_name} are malformed"
                )
            compact[field_name] = [normalise_slice(value) for value in raw_values]

    materialization = compact.get("materialization_input")
    if isinstance(materialization, Mapping):
        materialization_copy = dict(materialization)
        raw_values = materialization_copy.get("slices")
        if raw_values is not None:
            if not isinstance(raw_values, list):
                raise PurchaseControlCompactPayloadError(
                    "legacy compact purchase BUY materialization slices are malformed"
                )
            materialization_copy["slices"] = [normalise_slice(value) for value in raw_values]
        compact["materialization_input"] = materialization_copy
    return compact


def _load_parent_compact_purchase_rows(
    db: Session,
    *,
    parent_generation_id: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the complete accepted purchase scope for bounded row reuse.

    The physical publisher must still hand a complete scope to the current
    writer, but unchanged rows are already the accepted truth.  Reusing that
    manifest avoids re-running supplier coverage/custody math for every BUY
    owner on each physical tick.
    """
    from app.services.item_ledger.current_execution import (
        get_current_execution_scope,
        load_current_execution_rows,
    )

    scope = get_current_execution_scope(
        db,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )
    if (
        scope is None
        or not bool(scope.result_ready)
        or int(scope.source_generation_id or 0) != int(parent_generation_id)
    ):
        raise PurchaseControlCompactPayloadError(
            "compact purchase parent scope is missing or stale"
        )
    rows: list[dict[str, Any]] = []
    for current in load_current_execution_rows(
        db,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    ):
        payload = current.payload if isinstance(current.payload, Mapping) else None
        if not isinstance(payload, Mapping):
            raise PurchaseControlCompactPayloadError(
                "compact purchase parent row is malformed"
            )
        row = dict(payload)
        row.setdefault("row_key", str(current.business_identity))
        rows.append(row)

    buy_rows = [
        row for row in rows if row.get("row_generator") == _BUY_ROW_GENERATOR
    ]
    requirement_ids = sorted({
        int(value)
        for row in buy_rows
        for value in (row.get("requirement_ids") or [])
        if value not in (None, "")
    })
    if buy_rows and not requirement_ids:
        raise PurchaseControlCompactPayloadError(
            "legacy compact purchase BUY manifest has no requirements"
        )
    reservation_by_requirement: dict[int, models.ReservationEntry] = {}
    if requirement_ids:
        owners = (
            db.query(models.ReservationEntry)
            .filter(
                models.ReservationEntry.requirement_id.in_(requirement_ids),
                models.ReservationEntry.is_current.is_(True),
                models.ReservationEntry.owner_kind == "current",
                models.ReservationEntry.lifecycle_status == "active",
                models.ReservationEntry.realization_mode == _BUY_MODE,
            )
            .order_by(models.ReservationEntry.requirement_id.asc(), models.ReservationEntry.id.asc())
            .all()
        )
        for reservation in owners:
            requirement_id = int(reservation.requirement_id)
            expected_identity = reservation_business_identity(requirement_id, _BUY_MODE)
            if _clean_ref(reservation.current_identity) != expected_identity:
                raise PurchaseControlCompactPayloadError(
                    f"current BUY owner identity is malformed for requirement {requirement_id}"
                )
            if requirement_id in reservation_by_requirement:
                raise PurchaseControlCompactPayloadError(
                    f"current BUY owner is ambiguous for requirement {requirement_id}"
                )
            reservation_by_requirement[requirement_id] = reservation
        missing = [
            requirement_id
            for requirement_id in requirement_ids
            if requirement_id not in reservation_by_requirement
        ]
        if missing:
            # Report the full size first: the truncated head alone made an
            # operator read a whole-scope promotion failure as a handful of
            # stragglers.  The head stays for a directly actionable example.
            elision = " ..." if len(missing) > 8 else ""
            raise PurchaseControlCompactPayloadError(
                f"current BUY owner is missing for requirements {missing[:8]}"
                f"{elision} ({len(missing)} total)"
            )
        normalised_rows: list[dict[str, Any]] = []
        for row in rows:
            if row.get("row_generator") != _BUY_ROW_GENERATOR:
                normalised_rows.append(row)
                continue
            has_stable_identity = (
                isinstance(row.get("current_reservation_identities"), list)
                and bool(row.get("current_reservation_identities"))
            )
            has_staged_identity = any(
                isinstance(value, Mapping) and "work_item_id" in value
                for field_name in ("slices", "horizon_buckets")
                for value in (row.get(field_name) or [])
            )
            materialization = row.get("materialization_input")
            has_staged_identity = has_staged_identity or any(
                isinstance(value, Mapping) and "work_item_id" in value
                for value in (
                    materialization.get("slices", [])
                    if isinstance(materialization, Mapping)
                    else []
                )
            )
            if has_stable_identity and not has_staged_identity:
                normalised_rows.append(row)
            else:
                normalised_rows.append(
                    _normalise_legacy_parent_buy_row(
                        row,
                        reservation_by_requirement=reservation_by_requirement,
                    )
                )
        rows = normalised_rows
    summary = dict(scope.summary or {})
    cards = summary.get("cards")
    return rows, dict(cards) if isinstance(cards, Mapping) else {}


def _has_active_current_buy_owners(db: Session, *, run_ids: Sequence[int]) -> bool:
    """Does any live BUY owner exist that the purchase scope must describe?

    This is the same owner predicate the ordinary builder selects rows from;
    it is only asked to distinguish "the parent scope is legitimately empty"
    from "the parent scope lost its rows".
    """

    if not run_ids:
        return False
    return db.query(models.ReservationEntry.id).filter(
        models.ReservationEntry.is_current.is_(True),
        models.ReservationEntry.owner_kind == "current",
        models.ReservationEntry.lifecycle_status == "active",
        models.ReservationEntry.realization_mode == _BUY_MODE,
        models.ReservationEntry.current_identity != "",
        models.ReservationEntry.run_id.in_(tuple(int(value) for value in run_ids)),
    ).first() is not None


def validate_compact_current_purchase_control_payload(
    payload: Mapping[str, Any],
    target_generation: models.LedgerGeneration,
) -> None:
    """Validate a direct compact purchase payload without staging reads."""

    meta = payload.get("meta") if isinstance(payload, Mapping) else None
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if (
        not isinstance(meta, Mapping)
        or meta.get("read_only") is not True
        or str(meta.get("truth_status") or "") != "building"
        or int(meta.get("ledger_generation_id") or -1) != int(target_generation.id)
        or not isinstance(rows, list)
    ):
        raise PurchaseControlCompactPayloadError(
            "compact purchase payload metadata or rows are malformed"
        )
    try:
        row_count = int(meta.get("row_count"))
    except (TypeError, ValueError) as exc:
        raise PurchaseControlCompactPayloadError(
            "compact purchase payload row count is malformed"
        ) from exc
    if row_count < 0 or row_count != len(rows):
        raise PurchaseControlCompactPayloadError(
            "compact purchase payload row count is incomplete"
        )
    identities: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PurchaseControlCompactPayloadError(
                "compact purchase payload row is malformed"
            )
        row_key = str(row.get("row_key") or "")
        if not row_key or row_key in identities:
            raise PurchaseControlCompactPayloadError(
                "compact purchase payload row identity is duplicated"
            )
        identities.add(row_key)
        if row.get("row_generator") == _BUY_ROW_GENERATOR:
            validate_purchase_control_journal_buy_row(row)
            stable = row.get("current_reservation_identities")
            if not isinstance(stable, list) or not stable or any(
                not str(value).strip() for value in stable
            ):
                raise PurchaseControlCompactPayloadError(
                    "compact purchase BUY row lacks stable reservation identity"
                )
            if any("work_item_id" in dict(value) for value in row.get("slices", [])):
                raise PurchaseControlCompactPayloadError(
                    "compact purchase BUY row leaks staged work identity"
                )
        else:
            validate_purchase_control_journal_supply_row(row)


def build_compact_current_purchase_control_payload(
    db: Session,
    *,
    target_generation_id: int,
    parent_generation_id: int,
    accepted_run_ids: Sequence[int],
    affected_scopes: Iterable[tuple[int, str, str, str, str]] | None = None,
    reuse_parent_current: bool = False,
) -> dict[str, Any]:
    """Build purchase-control DTOs from current BUY owners and current supply.

    The BUILDING target is only a candidate boundary.  Stable current
    ``ReservationEntry`` owners, ``LedgerFutureSupplyCurrent`` supplier lines,
    and canonical ``_build_buyer_rows`` math provide the business payload.  No
    target-generation ``ReplenishmentWorkItem`` or reservation rows are read or
    written, and this function never moves planning truth or publishes rows.
    """

    target = db.get(models.LedgerGeneration, int(target_generation_id))
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if target is None or str(target.status or "") != "building":
        raise PurchaseControlCompactPayloadError(
            "compact purchase payload requires a BUILDING target"
        )
    if parent is None or str(parent.status or "") != "accepted":
        raise PurchaseControlCompactPayloadError(
            "compact purchase payload requires an accepted parent"
        )
    if target.id == parent.id or parent.cutoff is None or target.cutoff is None:
        raise PurchaseControlCompactPayloadError(
            "compact purchase payload generations are malformed"
        )
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or int(pointer.current_generation_id or -1) != int(parent.id):
        raise PurchaseControlCompactPayloadError(
            "compact purchase payload parent is not current truth"
        )
    run_ids = tuple(sorted({int(value) for value in accepted_run_ids}))
    if not run_ids:
        raise PurchaseControlCompactPayloadError(
            "compact purchase payload requires fixed live run ids"
        )
    scopes = _normalise_compact_purchase_scopes(affected_scopes)
    parent_rows: list[dict[str, Any]] = []
    parent_cards: dict[str, Any] = {}
    reused_row_count = 0
    bootstrap = False
    if reuse_parent_current:
        if scopes is None:
            raise PurchaseControlCompactPayloadError(
                "bounded compact purchase refresh requires explicit affected scopes"
            )
        parent_rows, parent_cards = _load_parent_compact_purchase_rows(
            db, parent_generation_id=int(parent.id)
        )
        if not parent_rows and _has_active_current_buy_owners(db, run_ids=run_ids):
            # The parent manifest is empty while live BUY owners exist: reusing
            # it would republish that emptiness for every later refresh.  A
            # migrated/repaired stand starts exactly here, so recompute the
            # complete scope once through the ordinary builder instead of
            # propagating the hole.
            bootstrap = True
            reuse_parent_current = False
            scopes = None
            parent_rows = []
            parent_cards = {}
        # A stock-only physical delta has no BUY scope.  Preserve the accepted
        # complete purchase payload without touching ReservationEvent,
        # custody, or future-supply history.
        elif not scopes:
            rows = list(parent_rows)
            rows.sort(
                key=lambda row: (
                    str(row.get("order_number") or ""),
                    str(row.get("item_code") or ""),
                    str(row.get("row_key") or ""),
                )
            )
            payload = {
                "meta": {
                    "ledger_generation_id": int(target.id),
                    "source_generation_id": int(parent.id),
                    "cutoff": target.cutoff.isoformat(),
                    "truth_status": "building",
                    "read_only": True,
                    "fact_source": "current",
                    "received_qty_status": "available",
                    "run_ids": list(run_ids),
                    "to_order_by_period": [],
                    "affected_scopes": [],
                    "row_count": len(rows),
                    "bounded_reuse": True,
                    "bootstrap": False,
                    "recomputed_scope_count": 0,
                    "reused_row_count": len(rows),
                },
                "rows": rows,
                "cards": parent_cards,
                "summary": {
                    "total_rows": len(rows),
                    "to_order": sum(
                        1 for row in rows
                        if str(row.get("line_status") or "") == "to_order"
                    ),
                    "fact_status": "available",
                },
            }
            validate_compact_current_purchase_control_payload(payload, target)
            return payload
    scope_keys = set(scopes or ())
    requested_item_pools = {
        (int(scope[0]), _clean_ref(scope[3]))
        for scope in scopes or ()
    }
    item_ids = sorted({scope[0] for scope in scopes}) if scopes else None
    owner_query = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.is_current.is_(True),
            models.ReservationEntry.owner_kind == "current",
            models.ReservationEntry.lifecycle_status == "active",
            models.ReservationEntry.realization_mode == _BUY_MODE,
            models.ReservationEntry.current_identity != "",
            models.ReservationEntry.run_id.in_(run_ids),
        )
    )
    if item_ids is not None:
        owner_query = owner_query.filter(models.ReservationEntry.item_id.in_(item_ids))
    reservations = owner_query.order_by(
        models.ReservationEntry.item_id.asc(),
        models.ReservationEntry.planning_stock_pool.asc(),
        models.ReservationEntry.id.asc(),
    ).all()
    identities: dict[str, int] = {}
    scope_by_item_pool: dict[tuple[int, str], tuple[int, str, str, str, str]] = {}
    recomputed_scope_set: set[tuple[int, str, str, str, str]] = set()
    for reservation in reservations:
        scope = (
            int(reservation.item_id),
            _clean_ref(reservation.characteristic_ref),
            _clean_ref(reservation.organization_ref),
            _clean_ref(reservation.planning_stock_pool),
            _BUY_MODE,
        )
        item_pool = (scope[0], scope[3])
        if scopes is not None:
            # Buyer rows are canonically grouped by item and planning pool,
            # not by characteristic/organization.  When reusing a complete
            # parent manifest, expand a requested scope to every current
            # owner in that item/pool group so an unaffected sibling cannot
            # be dropped from the aggregate row.  The ordinary compact
            # builder retains its exact-scope behavior.
            if reuse_parent_current:
                if item_pool not in requested_item_pools:
                    continue
            elif scope not in scope_keys:
                continue
        recomputed_scope_set.add(scope)
        previous_scope = scope_by_item_pool.get(item_pool)
        if (
            not reuse_parent_current
            and previous_scope is not None
            and previous_scope != scope
        ):
            raise PurchaseControlCompactPayloadError(
                "compact purchase owners have ambiguous characteristic or organization scope"
            )
        scope_by_item_pool[item_pool] = scope
        expected = reservation_business_identity(
            int(reservation.requirement_id), _BUY_MODE
        )
        identity = _clean_ref(reservation.current_identity)
        if identity != expected:
            raise PurchaseControlCompactPayloadError(
                "compact purchase owner has an ambiguous stable identity"
            )
        previous = identities.get(identity)
        if previous is not None and previous != int(reservation.id):
            raise PurchaseControlCompactPayloadError(
                "compact purchase owners contain duplicate stable identity"
            )
        identities[identity] = int(reservation.id)
    if scopes is not None:
        reservations = [
            row
            for row in reservations
            if (
                int(row.item_id),
                _clean_ref(row.characteristic_ref),
                _clean_ref(row.organization_ref),
                _clean_ref(row.planning_stock_pool),
                _BUY_MODE,
            ) in (
                recomputed_scope_set
                if reuse_parent_current
                else scope_keys
            )
        ]
        if reuse_parent_current and recomputed_scope_set:
            scopes = tuple(sorted(recomputed_scope_set))
    items = {
        int(item.item_id): item
        for item in db.query(models.Item)
        .filter(models.Item.item_id.in_(sorted({int(row.item_id) for row in reservations})))
        .all()
    }
    if len(items) != len({int(row.item_id) for row in reservations}):
        raise PurchaseControlCompactPayloadError(
            "compact purchase owner references missing item"
        )
    requirements = {
        int(requirement.id): requirement
        for requirement in db.query(models.MrpRequirement)
        .filter(models.MrpRequirement.id.in_(sorted({int(row.requirement_id) for row in reservations})))
        .all()
    }
    entries: list[tuple[Any, Any, Any]] = []
    identity_by_reservation: dict[int, str] = {}
    for reservation in reservations:
        if reservation.run_id is None or int(reservation.run_id) not in run_ids:
            raise PurchaseControlCompactPayloadError(
                "compact purchase owner has an invalid live run"
            )
        if int(reservation.requirement_id) not in requirements:
            raise PurchaseControlCompactPayloadError(
                "compact purchase owner has missing requirement lineage"
            )
        identity = _clean_ref(reservation.current_identity)
        identity_by_reservation[int(reservation.id)] = identity
        required = reservation.replenishment_required_qty or 0
        fulfilled = reservation.replenishment_received_qty or 0
        remaining = replenishment_remaining(required, fulfilled)
        synthetic_work = SimpleNamespace(
            id=-int(reservation.id),
            reservation_id=int(reservation.id),
            item_id=int(reservation.item_id),
            requirement_id=int(reservation.requirement_id),
            run_id=int(reservation.run_id),
            replenishment_required_qty=required,
            replenishment_fulfilled_qty=fulfilled,
            replenishment_remaining_qty=remaining,
        )
        entries.append((synthetic_work, reservation, items[int(reservation.item_id)]))

    to_order_by_period: list[dict[str, Any]] = []
    buyer_rows = _build_buyer_rows(
        db,
        int(parent.id),
        to_order_by_period,
        cutoff_date=target.cutoff.date(),
        entries_override=entries,
        affected_scopes=scopes,
    )
    buyer_rows = [
        _compact_purchase_row_without_work_item(
            dict(row), identity_by_reservation=identity_by_reservation
        )
        for row in buyer_rows
    ]
    supplier_rows, recomputed_cards = _build_supplier_card_rows(
        db,
        parent,
        affected_scopes=scopes,
        cutoff_date=target.cutoff.date(),
    )
    if reuse_parent_current:
        affected_item_pools = {
            (int(scope[0]), _clean_ref(scope[3]))
            for scope in scopes or ()
        }
        recomputed_row_keys = {
            str(row.get("row_key") or "")
            for row in [*supplier_rows, *buyer_rows]
            if str(row.get("row_key") or "")
        }
        unchanged_rows = [
            row for row in parent_rows
            if (
                int(row.get("item_id") or 0),
                _clean_ref(row.get("planning_stock_pool")),
            ) not in affected_item_pools
            and str(row.get("row_key") or "") not in recomputed_row_keys
        ]
        reused_row_count = len(unchanged_rows)
        replaced_order_ids: set[str] = set()
        for key, card in parent_cards.items():
            lines = card.get("lines", []) if isinstance(card, Mapping) else []
            if any(
                (
                    int(line.get("item_id") or 0),
                    _clean_ref(line.get("planning_stock_pool")),
                ) in affected_item_pools
                for line in lines
                if isinstance(line, Mapping)
            ):
                replaced_order_ids.add(str(key))
        replaced_order_ids.update(
            str(row.get("order_id"))
            for row in supplier_rows
            if row.get("order_id") not in (None, "")
        )
        preserved_cards = {
            key: value
            for key, value in parent_cards.items()
            if str(key) not in replaced_order_ids
        }
        preserved_cards.update(recomputed_cards)
        bounded_cards = preserved_cards
        rows = [*unchanged_rows, *supplier_rows, *buyer_rows]
    else:
        bounded_cards = recomputed_cards
        rows = [*supplier_rows, *buyer_rows]
    rows.sort(
        key=lambda row: (
            str(row.get("order_number") or ""),
            str(row.get("item_code") or ""),
            str(row.get("row_key") or ""),
        )
    )
    for row in rows:
        validate_purchase_control_journal_row(row)
    payload = {
        "meta": {
            "ledger_generation_id": int(target.id),
            "source_generation_id": int(parent.id),
            "cutoff": target.cutoff.isoformat(),
            "truth_status": "building",
            "read_only": True,
            "fact_source": "current",
            "received_qty_status": "available",
            "run_ids": list(run_ids),
            "to_order_by_period": to_order_by_period,
            "affected_scopes": [list(scope) for scope in scopes] if scopes else None,
            "row_count": len(rows),
            "bounded_reuse": bool(reuse_parent_current),
            "bootstrap": bool(bootstrap),
            "recomputed_scope_count": len(scopes or ()) if reuse_parent_current else None,
            "reused_row_count": reused_row_count,
        },
        "rows": rows,
        "cards": bounded_cards,
        "summary": {
            "total_rows": len(rows),
            "to_order": sum(
                1 for row in rows if str(row.get("line_status") or "") == "to_order"
            ),
            "fact_status": "available",
        },
    }
    validate_compact_current_purchase_control_payload(payload, target)
    return payload
