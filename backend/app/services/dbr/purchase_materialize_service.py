"""Materialize DBR purchasing decisions into 1С — Фаза 3 (закупочный контур).

Two inputs, one target document (``Document_ЗаказПоставщику``), one export path
(the proven ``one_c_purchase_order_export`` group/line machinery):

- ``launch_purchase_signals`` — open, complete «Пополнение» signals of *purchased*
  items become supplier orders, grouped by supplier (``items.supplier_ref1c``).
  Signals with no resolvable supplier are reported as ``unresolved`` and never
  block the batch.
- ``purchase_plan_preview`` — a *pure* net-requirement preview: demand exploded
  through the already-ported core ``aggregate_purchase_demand`` (drum/program →
  purchase leaves), netted against on-hand stock + open supplier orders. Reads
  only, writes nothing.
- ``materialize_purchase_plan`` — turns the preview rows into supplier orders via
  the same export path, keyed by item so a re-run is idempotent.

Invariants mirror ``materialize_service``:
- ``dry_run=True`` is the default — it returns the payload preview and writes
  nothing (no 1С call, no sync_link, no status change).
- Idempotency rides on ``sync_link`` (source_system='dbr',
  target_entity='Document_ЗаказПоставщику'): a source already exported with a
  success link is a no-op that keeps its stored ref.
- The shared ``one_c_purchase_order_export`` / ``supplier_order_sync`` files are
  not modified here (the feedback hook is a single guarded call).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from ...models import (
    DbrDrumSchedule,
    DbrFeederSignal,
    DbrProductionProgram,
    DbrProductionProgramItem,
    DbrDrumSlot,
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    StockWarehouse,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
)
from ..odata_client import OData1CClient
from ..odata_config import load_odata_config as _load_odata_config
from ..one_c_export_common import (
    add_origin_marker,
    clean_ref1c,
    create_odata_client,
    find_document_by_origin,
    fmt_1c_datetime as _fmt_1c_datetime,
    origin_token,
    payload_hash as _payload_hash,
)
from ..one_c_purchase_order_export import (
    PURCHASE_ORDER_ENTITY,
    PurchaseOrderExportGroup,
    PurchaseOrderExportLine,
    _order_lines_payload,
)
from ..replenishment import is_purchase_replenishment
from . import adapters
from . import classify as classify_mod
from . import cockpit_snapshot_service
from . import settings_service
from .core.drum import kit as kit_mod
from .core.feeder import demand_explosion, signal_identity
from .core.purchase_demand import aggregate_purchase_demand

SOURCE_SYSTEM = "dbr"
SIGNAL_DOCTYPE = "feeder_signal"
PLAN_DOCTYPE = "purchase_plan"


# ---------------------------------------------------------------------------
# sync_link helpers (source_system='dbr', target_entity=Document_ЗаказПоставщику)
# ---------------------------------------------------------------------------


def _find_dbr_purchase_link(
    db: Session, *, source_doctype: str, source_id: int
) -> Optional[SyncLink]:
    return (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == SOURCE_SYSTEM,
            SyncLink.source_doctype == source_doctype,
            SyncLink.source_id == int(source_id),
            SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
        )
        .one_or_none()
    )


def _upsert_dbr_purchase_link(
    db: Session,
    *,
    source_doctype: str,
    source_id: int,
    target_number: str,
    payload_hash: str,
    target_ref_key: Optional[str],
    status: str,
    last_error: Optional[str],
) -> None:
    existing = _find_dbr_purchase_link(
        db, source_doctype=source_doctype, source_id=source_id
    )
    synced_at = datetime.now(timezone.utc) if status == "success" else None
    if existing is None:
        db.add(
            SyncLink(
                source_system=SOURCE_SYSTEM,
                source_doctype=source_doctype,
                source_id=int(source_id),
                target_system="1C",
                target_entity=PURCHASE_ORDER_ENTITY,
                target_ref_key=target_ref_key,
                target_number=target_number,
                payload_hash=payload_hash,
                status=status,
                last_error=last_error,
                last_synced_at=synced_at,
            )
        )
        return
    existing.target_number = target_number
    existing.payload_hash = payload_hash
    existing.status = status
    existing.last_error = last_error
    if target_ref_key:
        existing.target_ref_key = target_ref_key
    if synced_at is not None:
        existing.last_synced_at = synced_at


def _already_exported_ids(db: Session, source_doctype: str) -> set[int]:
    return {
        int(sid)
        for (sid,) in db.query(SyncLink.source_id)
        .filter(
            SyncLink.source_system == SOURCE_SYSTEM,
            SyncLink.source_doctype == source_doctype,
            SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
            SyncLink.status == "success",
        )
        .all()
    }


# ---------------------------------------------------------------------------
# Shared writer: group of supplier lines → one Document_ЗаказПоставщику.
# ---------------------------------------------------------------------------


def _group_number(prefix: str, seed: int) -> str:
    # <=11 chars to fit Document_ЗаказПоставщику.Number; seed is stable per group
    # (min source id), so a re-export re-derives the same number.
    return f"{prefix}{int(seed) % 100000:05d}"


def _post_purchase_group(
    db: Session,
    *,
    client: Any,
    group: PurchaseOrderExportGroup,
    source_doctype: str,
    source_ids_of_line: Callable[[PurchaseOrderExportLine], list[int]],
    stamp: Callable[[int, str, str], None],
) -> None:
    """POST one supplier order, stamp a sync_link per source id, run ``stamp``.

    Raises on 1С failure; the caller records the group error and moves on.
    """
    min_need = min(
        (date.fromisoformat(line.need_date) for line in group.lines if line.need_date),
        default=None,
    )
    source_ids = sorted(
        {
            int(source_id)
            for line in group.lines
            for source_id in source_ids_of_line(line)
        }
    )
    if source_doctype == SIGNAL_DOCTYPE:
        durable_sources = sorted(
            str(value)
            for (value,) in db.query(DbrFeederSignal.dedup_key)
            .filter(DbrFeederSignal.id.in_(source_ids))
            .all()
        )
    else:
        # Purchase-plan rows have no durable cross-instance delta UUID. Do not
        # leak local ids into the marker; line/date/qty axes below are the best
        # available identity and the identical-delta limitation is documented.
        durable_sources = []
    token = origin_token(
        f"dbr-purchase:{source_doctype}",
        {
            "supplier_ref1c": group.supplier_ref1c,
            "sources": durable_sources,
            "lines": [
                {
                    "item_ref1c": line.item_ref1c,
                    "qty": float(line.qty or 0),
                    "need_date": line.need_date,
                }
                for line in group.lines
            ],
        },
    )
    comment = add_origin_marker(
        f"PRODPLAN source={source_doctype}; dbr; number={group.number}",
        token,
    )
    header = {
        "Number": group.number,
        "Date": _fmt_1c_datetime(date.today()),
        "Posted": False,
        "Контрагент_Key": group.supplier_ref1c,
        "ДатаПоступления": _fmt_1c_datetime(min_need),
        "Комментарий": comment,
        "Запасы": _order_lines_payload("", group),
    }
    existing = find_document_by_origin(
        client,
        entity=PURCHASE_ORDER_ENTITY,
        token=token,
    )
    ref_key = clean_ref1c((existing or {}).get("Ref_Key"))
    if not ref_key:
        created = client.post(PURCHASE_ORDER_ENTITY, header)
        ref_key = clean_ref1c(created.get("Ref_Key"))
    if not ref_key:
        raise RuntimeError(
            f"1C did not return Ref_Key for the new {PURCHASE_ORDER_ENTITY}"
        )
    group.target_ref_key = ref_key
    group.status = "existing" if existing else "created"
    phash = _payload_hash(header)
    for line in group.lines:
        for source_id in source_ids_of_line(line):
            _upsert_dbr_purchase_link(
                db,
                source_doctype=source_doctype,
                source_id=source_id,
                target_number=group.number,
                payload_hash=phash,
                target_ref_key=ref_key,
                status="success",
                last_error=None,
            )
            stamp(source_id, ref_key, group.number)


# ---------------------------------------------------------------------------
# launch_purchase_signals
# ---------------------------------------------------------------------------


def _is_purchase_signal(signal: DbrFeederSignal) -> bool:
    item = signal.item
    return bool(item and is_purchase_replenishment(item.replenishment_method))


def _require_snapshot_signal_selection(
    db: Session,
    signal_ids: Optional[list[int]],
) -> tuple[list[int], int, int]:
    """Validate one explicit selection against the accepted cockpit snapshot."""
    if signal_ids is None:
        raise ValueError("signal_ids must be an explicit non-empty list")
    try:
        selected = [int(signal_id) for signal_id in signal_ids]
    except (TypeError, ValueError) as exc:
        raise ValueError("signal_ids must contain positive integer IDs") from exc
    if not selected:
        raise ValueError("signal_ids must be an explicit non-empty list")
    if any(signal_id <= 0 for signal_id in selected):
        raise ValueError("signal_ids must contain positive integer IDs")
    if len(selected) != len(set(selected)):
        raise ValueError("signal_ids must not contain duplicates")

    cockpit = cockpit_snapshot_service.read_cockpit_snapshot(db)
    meta = cockpit.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("current DBR cockpit snapshot has no lineage metadata")
    try:
        generation_id = int(meta["ledger_generation"])
        snapshot_id = int(meta["snapshot_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("current DBR cockpit snapshot has invalid lineage metadata") from exc

    snapshot_signal_ids: set[int] = set()
    for row in cockpit.get("signals", []):
        if not isinstance(row, dict):
            continue
        value = row.get("id")
        if isinstance(value, bool):
            continue
        try:
            signal_id = int(value)
        except (TypeError, ValueError):
            continue
        try:
            row_generation_id = int(row.get("ledger_generation_id"))
        except (TypeError, ValueError):
            continue
        if signal_id > 0 and row_generation_id == generation_id:
            snapshot_signal_ids.add(signal_id)
    absent = sorted(set(selected) - snapshot_signal_ids)
    if absent:
        raise ValueError(
            "signal_ids are absent from current accepted DBR cockpit snapshot: "
            + ", ".join(str(signal_id) for signal_id in absent)
        )

    exact_live_ids = {
        int(signal_id)
        for (signal_id,) in db.query(DbrFeederSignal.id)
        .filter(
            DbrFeederSignal.id.in_(selected),
            DbrFeederSignal.ledger_generation_id == generation_id,
        )
        .all()
    }
    stale = sorted(set(selected) - exact_live_ids)
    if stale:
        raise ValueError(
            "signal_ids have no live row in the snapshot Ledger generation: "
            + ", ".join(str(signal_id) for signal_id in stale)
        )
    return sorted(selected), generation_id, snapshot_id


def launch_purchase_signals(
    db: Session,
    signal_ids: Optional[list[int]] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Launch open «Пополнение» signals of purchased items → supplier orders.

    - ``signal_ids`` is mandatory and must name exact rows from the current
      accepted ``dbr_feeder_cockpit`` snapshot.
    - Lines are grouped by supplier (``items.supplier_ref1c``); qty is
      ``suggested_qty`` and the receipt date is ``today + replenishment_time``.
    - Signals without a supplier (or without item_ref1c) are reported under
      ``unresolved`` and never fail the batch.
    - dry_run=True (default): returns the grouped payload preview, writes nothing.
    - dry_run=False: creates one Document_ЗаказПоставщику per supplier, stamps a
      sync_link per signal, and moves each signal to 'Order Created'.
    """
    selected, generation_id, snapshot_id = _require_snapshot_signal_selection(
        db, signal_ids
    )
    q = db.query(DbrFeederSignal).filter(
        DbrFeederSignal.id.in_(selected),
        DbrFeederSignal.ledger_generation_id == generation_id,
        DbrFeederSignal.signal_type == "Пополнение",
        DbrFeederSignal.status == signal_identity.OPEN,
        DbrFeederSignal.is_incomplete.is_(False),
        DbrFeederSignal.suggested_qty > 0,
    )
    q = q.order_by(DbrFeederSignal.id.asc())

    already = _already_exported_ids(db, SIGNAL_DOCTYPE)
    unresolved: list[dict[str, Any]] = []
    already_report: list[dict[str, Any]] = []
    today = date.today()

    # supplier_ref -> {(item_id): PurchaseOrderExportLine}
    grouped: dict[str, dict[int, PurchaseOrderExportLine]] = {}
    signals_by_id: dict[int, DbrFeederSignal] = {}

    for signal in q.all():
        if not _is_purchase_signal(signal):
            continue
        if int(signal.id) in already:
            link = _find_dbr_purchase_link(
                db, source_doctype=SIGNAL_DOCTYPE, source_id=signal.id
            )
            already_report.append(
                {
                    "signal_id": int(signal.id),
                    "one_c_order_ref": clean_ref1c(link.target_ref_key) if link else None,
                    "one_c_order_number": link.target_number if link else None,
                }
            )
            continue
        item = signal.item
        supplier_ref = clean_ref1c(item.supplier_ref1c) if item else ""
        item_ref = clean_ref1c(item.item_ref1c) if item else ""
        qty = float(signal.suggested_qty or 0)
        if not supplier_ref or not item_ref or qty <= 0:
            unresolved.append(
                {
                    "signal_id": int(signal.id),
                    "item_id": int(signal.item_id),
                    "item_name": item.item_name if item else None,
                    "missing_supplier": not bool(supplier_ref),
                    "missing_item_ref1c": not bool(item_ref),
                }
            )
            continue
        rt = int(item.replenishment_time or 0)
        need = today + timedelta(days=rt)
        signals_by_id[int(signal.id)] = signal
        bucket = grouped.setdefault(supplier_ref, {})
        line = bucket.get(int(signal.item_id))
        if line is None:
            bucket[int(signal.item_id)] = PurchaseOrderExportLine(
                purchase_ids=[int(signal.id)],
                item_id=int(signal.item_id),
                item_ref1c=item_ref,
                item_name=item.item_name or "",
                item_article=item.item_article or "",
                unit_ref1c=clean_ref1c(item.unit),
                unit_name="",
                qty=qty,
                need_date=need.isoformat(),
                order_date=today.isoformat(),
            )
        else:
            line.purchase_ids.append(int(signal.id))
            line.qty += qty
            # keep the earliest receipt date for the merged line
            if line.need_date is None or need.isoformat() < line.need_date:
                line.need_date = need.isoformat()

    groups: list[PurchaseOrderExportGroup] = []
    for index, supplier_ref in enumerate(sorted(grouped.keys()), start=1):
        lines = sorted(
            grouped[supplier_ref].values(),
            key=lambda ln: (ln.item_name.lower(), ln.item_article.lower()),
        )
        seed = min(sid for ln in lines for sid in ln.purchase_ids)
        groups.append(
            PurchaseOrderExportGroup(
                supplier_ref1c=supplier_ref,
                number=_group_number("DBRPS", seed),
                lines=lines,
            )
        )

    base = {
        "ok": True,
        "dry_run": bool(dry_run),
        "kind": "feeder_purchase",
        "entity": PURCHASE_ORDER_ENTITY,
        "snapshot_id": snapshot_id,
        "ledger_generation": generation_id,
        "orders_planned": len(groups),
        "signals_total": sum(len(ln.purchase_ids) for g in groups for ln in g.lines),
        "unresolved": unresolved,
        "already_exported": already_report,
    }

    if dry_run:
        return {
            **base,
            "orders_created": 0,
            "orders": [_group_out(g) for g in groups],
            "note": "dry-run: предпросмотр payload, запись не выполнялась",
        }

    client = create_odata_client(
        _load_odata_config(), OData1CClient, allow_production=True, require_demo_base=True
    )

    def _stamp(signal_id: int, ref_key: str, number: str) -> None:
        signal = signals_by_id.get(signal_id)
        if signal is None:
            return
        signal.one_c_order_ref = ref_key
        signal.one_c_order_number = number
        signal.status = signal_identity.ORDER_CREATED

    created = 0
    errors = 0
    for group in groups:
        try:
            _post_purchase_group(
                db,
                client=client,
                group=group,
                source_doctype=SIGNAL_DOCTYPE,
                source_ids_of_line=lambda ln: list(ln.purchase_ids),
                stamp=_stamp,
            )
            created += 1
        except Exception as exc:  # noqa: BLE001 — isolate one supplier's failure
            group.status = "error"
            group.error = str(exc)
            errors += 1

    db.flush()
    return {
        **base,
        "ok": errors == 0,
        "orders_created": created,
        "errors": errors,
        "orders": [_group_out(g) for g in groups],
    }


def _group_out(group: PurchaseOrderExportGroup) -> dict[str, Any]:
    return {
        "supplier_ref1c": group.supplier_ref1c,
        "number": group.number,
        "status": group.status,
        "target_ref_key": group.target_ref_key,
        "error": group.error,
        "lines": [
            {
                "item_id": ln.item_id,
                "item_ref1c": ln.item_ref1c,
                "item_name": ln.item_name,
                "qty": float(ln.qty or 0),
                "need_date": ln.need_date,
                "order_date": ln.order_date,
                "source_ids": list(ln.purchase_ids),
            }
            for ln in group.lines
        ],
    }


# ---------------------------------------------------------------------------
# purchase_plan_preview — pure net-requirement preview (no writes)
# ---------------------------------------------------------------------------


def _slot_demand_from_program(
    db: Session, program_id: int, id_to_code: dict[int, str]
) -> list[tuple[str, float, Optional[date]]]:
    program = db.get(DbrProductionProgram, int(program_id))
    if program is None:
        raise LookupError("program not found")
    demand: list[tuple[str, float, Optional[date]]] = []
    for row in (
        db.query(DbrProductionProgramItem)
        .filter(DbrProductionProgramItem.program_id == int(program_id))
        .all()
    ):
        code = id_to_code.get(int(row.item_id))
        if code is None:
            continue
        demand.append((code, float(row.qty or 0), row.program_date))
    return demand


def _slot_demand_from_active_schedule(
    db: Session, id_to_code: dict[int, str]
) -> list[tuple[str, float, Optional[date]]]:
    schedule = (
        db.query(DbrDrumSchedule)
        .filter(DbrDrumSchedule.status == "active")
        .one_or_none()
    )
    if schedule is None:
        raise ValueError("нет активного графика барабана")
    demand: list[tuple[str, float, Optional[date]]] = []
    for slot in (
        db.query(DbrDrumSlot).filter(DbrDrumSlot.schedule_id == schedule.id).all()
    ):
        code = id_to_code.get(int(slot.item_id))
        if code is None:
            continue
        demand.append((code, float(slot.qty or 0), slot.slot_date))
    return demand


def _purchase_lines_provider(db: Session):
    """Return (lines_of_sku, notes): kit lines flagged is_purchase per component.

    ``lines_of_sku(sku_code)`` explodes the kit through every decoupling level
    (``demand_explosion.explode_kit``) and tags each boundary line by whether its
    item is a purchased component. ``aggregate_purchase_demand`` keeps only the
    purchased lines, so manufactured buffers pass through transparently to their
    purchased descendants.
    """
    settings = settings_service.get_or_create_settings(db)
    classify, notes = classify_mod.build_classifier(db, settings)
    components_of = adapters.build_components_provider(db)
    items = {row.item_code: row for row in db.query(Item).all()}

    node_cache: dict[str, demand_explosion.Node] = {}

    def node_for(code: str) -> demand_explosion.Node:
        cached = node_cache.get(code)
        if cached is not None:
            return cached
        decision, warehouse = classify(code)
        if decision == kit_mod.FASTENER:
            node = demand_explosion.Node(demand_explosion.SKIP)
        elif decision == kit_mod.RECURSE:
            node = demand_explosion.Node(demand_explosion.RECURSE)
        else:
            item = items.get(code)
            make = bool(item and not is_purchase_replenishment(item.replenishment_method))
            node = demand_explosion.Node(
                demand_explosion.BOUNDARY,
                warehouse,
                explode_through=make and bool(list(components_of(code))),
            )
        node_cache[code] = node
        return node

    memo: dict[str, list] = {}

    def lines_of_sku(sku: str):
        out = []
        for kit_line in demand_explosion.explode_kit(
            sku, components_of, node_for, memo=memo
        ):
            item = items.get(kit_line.item)
            is_purchase = bool(
                item and is_purchase_replenishment(item.replenishment_method)
            )
            out.append((kit_line.item, kit_line.qty_per_unit, is_purchase))
        return out

    return lines_of_sku, notes, items


def _onhand_by_item_id(db: Session, item_ids: set[int]) -> dict[int, float]:
    """On-hand per item across selected, non-ignored warehouses (adapters policy)."""
    if not item_ids:
        return {}
    ignored = {r[0] for r in db.query(IgnoredWarehouse.warehouse_ref1c).all() if r[0]}
    wh_rows = db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.is_selected).all()
    has_settings = bool(wh_rows)
    selected = {ref for ref, sel in wh_rows if ref and bool(sel)}
    result: dict[int, float] = {}
    for iid, ref, qty in (
        db.query(
            ItemWarehouseStock.item_id,
            ItemWarehouseStock.warehouse_ref1c,
            ItemWarehouseStock.qty,
        )
        .filter(ItemWarehouseStock.item_id.in_(sorted(item_ids)))
        .all()
    ):
        if ref in ignored:
            continue
        if has_settings and ref not in selected:
            continue
        result[int(iid)] = result.get(int(iid), 0.0) + float(qty or 0)
    return result


def _open_po_remaining_by_item_id(db: Session, item_ids: set[int]) -> dict[int, float]:
    """Remaining qty of open (non-deleted) supplier orders, per item."""
    if not item_ids:
        return {}
    result: dict[int, float] = {}
    for line, _order in (
        db.query(SupplierOrderItem, SupplierOrder)
        .join(SupplierOrder, SupplierOrderItem.order_id == SupplierOrder.order_id)
        .filter(
            SupplierOrderItem.item_id_ref.in_(sorted(item_ids)),
            SupplierOrderItem.remaining_qty > 0,
            SupplierOrder.deletion_mark.is_(False),
        )
        .all()
    ):
        iid = int(line.item_id_ref)
        result[iid] = result.get(iid, 0.0) + float(line.remaining_qty or 0)
    return result


def purchase_plan_preview(
    db: Session,
    *,
    program_id: Optional[int] = None,
    active_schedule: bool = False,
    lead_time_threshold_days: int = 60,
) -> dict[str, Any]:
    """Net purchase requirement of a program or the active drum schedule.

    Gross demand is exploded through ``aggregate_purchase_demand`` (purchased
    leaves only) and netted against on-hand stock + open supplier orders. The
    result is a pure preview — nothing is written. ``order_before`` is
    ``need_date - replenishment_time``; ``within_lead_time_threshold`` marks the
    rows whose order-by date already falls inside ``today + threshold`` (act now).
    """
    if program_id is None and not active_schedule:
        raise ValueError("нужен program_id или active_schedule")

    id_to_code, code_to_id = adapters.item_code_maps(db)
    if program_id is not None:
        source = {"kind": "program", "program_id": int(program_id)}
        slot_demand = _slot_demand_from_program(db, int(program_id), id_to_code)
    else:
        source = {"kind": "active_schedule"}
        slot_demand = _slot_demand_from_active_schedule(db, id_to_code)

    lines_of_sku, notes, items_by_code = _purchase_lines_provider(db)
    aggregate = aggregate_purchase_demand(slot_demand, lines_of_sku)

    demand_item_ids = {
        int(items_by_code[code].item_id)
        for code in aggregate
        if code in items_by_code
    }
    onhand = _onhand_by_item_id(db, demand_item_ids)
    open_po = _open_po_remaining_by_item_id(db, demand_item_ids)

    today = date.today()
    threshold_date = today + timedelta(days=int(lead_time_threshold_days))
    rows: list[dict[str, Any]] = []
    for code, entry in aggregate.items():
        item = items_by_code.get(code)
        if item is None:
            continue
        iid = int(item.item_id)
        demand_qty = float(entry["qty"] or 0)
        stock = float(onhand.get(iid, 0.0))
        inbound = float(open_po.get(iid, 0.0))
        available = stock + inbound
        to_order = max(0.0, demand_qty - available)
        need_date = entry["earliest_need_date"]
        rt = int(item.replenishment_time or 0)
        order_before = (need_date - timedelta(days=rt)) if need_date else None
        rows.append(
            {
                "item_id": iid,
                "item_code": item.item_code,
                "item_name": item.item_name,
                "supplier_ref1c": clean_ref1c(item.supplier_ref1c) or None,
                "demand_qty": round(demand_qty, 3),
                "stock_qty": round(stock, 3),
                "open_order_qty": round(inbound, 3),
                "available_qty": round(available, 3),
                "to_order_qty": round(to_order, 3),
                "need_date": need_date.isoformat() if need_date else None,
                "replenishment_time": rt,
                "order_before": order_before.isoformat() if order_before else None,
                "within_lead_time_threshold": bool(
                    order_before is not None and order_before <= threshold_date
                ),
            }
        )

    rows.sort(key=lambda r: (r["order_before"] or "9999-12-31", r["item_code"]))
    to_order_rows = [r for r in rows if r["to_order_qty"] > 0]
    return {
        "ok": True,
        "source": source,
        "lead_time_threshold_days": int(lead_time_threshold_days),
        "rows": rows,
        "rows_to_order": len(to_order_rows),
        "items_total": len(rows),
        "warnings": list(dict.fromkeys(notes)),
    }


# ---------------------------------------------------------------------------
# materialize_purchase_plan — preview rows → supplier orders (same export path)
# ---------------------------------------------------------------------------


def materialize_purchase_plan(
    db: Session,
    *,
    program_id: Optional[int] = None,
    active_schedule: bool = False,
    lead_time_threshold_days: int = 60,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Materialize the net purchase plan into supplier orders (same export path).

    Runs ``purchase_plan_preview`` then groups the to-order rows by supplier.
    Rows without a supplier are reported as ``unresolved``. Idempotency rides on
    a sync_link keyed by ``item_id`` (source_doctype='purchase_plan'): an item
    already exported is skipped. dry_run=True (default) previews only.
    """
    preview = purchase_plan_preview(
        db,
        program_id=program_id,
        active_schedule=active_schedule,
        lead_time_threshold_days=lead_time_threshold_days,
    )
    already = _already_exported_ids(db, PLAN_DOCTYPE)
    unresolved: list[dict[str, Any]] = []
    already_report: list[dict[str, Any]] = []
    grouped: dict[str, list[PurchaseOrderExportLine]] = {}

    for row in preview["rows"]:
        if row["to_order_qty"] <= 0:
            continue
        iid = int(row["item_id"])
        if iid in already:
            link = _find_dbr_purchase_link(
                db, source_doctype=PLAN_DOCTYPE, source_id=iid
            )
            already_report.append(
                {
                    "item_id": iid,
                    "one_c_order_ref": clean_ref1c(link.target_ref_key) if link else None,
                    "one_c_order_number": link.target_number if link else None,
                }
            )
            continue
        supplier_ref = clean_ref1c(row["supplier_ref1c"] or "")
        item = db.get(Item, iid)
        item_ref = clean_ref1c(item.item_ref1c) if item else ""
        if not supplier_ref or not item_ref:
            unresolved.append(
                {
                    "item_id": iid,
                    "item_name": row["item_name"],
                    "missing_supplier": not bool(supplier_ref),
                    "missing_item_ref1c": not bool(item_ref),
                }
            )
            continue
        grouped.setdefault(supplier_ref, []).append(
            PurchaseOrderExportLine(
                purchase_ids=[iid],
                item_id=iid,
                item_ref1c=item_ref,
                item_name=row["item_name"] or "",
                item_article=item.item_article or "" if item else "",
                unit_ref1c=clean_ref1c(item.unit) if item else "",
                unit_name="",
                qty=float(row["to_order_qty"]),
                need_date=row["need_date"],
                order_date=row["order_before"],
            )
        )

    groups: list[PurchaseOrderExportGroup] = []
    for supplier_ref in sorted(grouped.keys()):
        lines = sorted(
            grouped[supplier_ref],
            key=lambda ln: (ln.item_name.lower(), ln.item_article.lower()),
        )
        seed = min(ln.item_id for ln in lines)
        groups.append(
            PurchaseOrderExportGroup(
                supplier_ref1c=supplier_ref,
                number=_group_number("DBRPP", seed),
                lines=lines,
            )
        )

    base = {
        "ok": True,
        "dry_run": bool(dry_run),
        "kind": "purchase_plan",
        "entity": PURCHASE_ORDER_ENTITY,
        "source": preview["source"],
        "orders_planned": len(groups),
        "items_total": sum(len(g.lines) for g in groups),
        "unresolved": unresolved,
        "already_exported": already_report,
    }

    if dry_run:
        return {
            **base,
            "orders_created": 0,
            "orders": [_group_out(g) for g in groups],
            "note": "dry-run: предпросмотр payload, запись не выполнялась",
        }

    client = create_odata_client(
        _load_odata_config(), OData1CClient, allow_production=True, require_demo_base=True
    )

    def _stamp(_item_id: int, _ref_key: str, _number: str) -> None:
        # Plan rows have no owning row to advance; the sync_link is the record.
        return None

    created = 0
    errors = 0
    for group in groups:
        try:
            _post_purchase_group(
                db,
                client=client,
                group=group,
                source_doctype=PLAN_DOCTYPE,
                source_ids_of_line=lambda ln: list(ln.purchase_ids),
                stamp=_stamp,
            )
            created += 1
        except Exception as exc:  # noqa: BLE001 — isolate one supplier's failure
            group.status = "error"
            group.error = str(exc)
            errors += 1

    db.flush()
    return {
        **base,
        "ok": errors == 0,
        "orders_created": created,
        "errors": errors,
        "orders": [_group_out(g) for g in groups],
    }
