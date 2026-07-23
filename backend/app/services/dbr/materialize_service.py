"""Materialize DBR decisions into 1С — Фаза 3.

Turns a *released* drum slot or a *launched* feeder signal into a
Document_ЗаказНаПроизводство in 1С, reusing the proven MRP export machinery
(``one_c_production_order_export`` payload builder +
``one_c_export_common.post_export_entries``). Nothing new is invented on the 1С
side: the document shape is byte-for-byte the same as the MRP export, only the
inputs (slot / signal instead of production_orders) and the ``sync_link`` marker
(``source_system='dbr'``) differ.

Invariants:
- ``dry_run=True`` is the default everywhere — it returns the payload preview
  and writes nothing (no 1С call, no sync_link, no status change).
- Idempotency is carried by ``sync_link`` (source_system='dbr'): a second run
  over an already-exported slot/signal is a no-op that returns the stored ref.
- Only green + pending slots are releasable; only Open + complete feeder
  signals that pass the material ``can_launch`` gate are launchable.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from ...models import (
    DbrDrumSchedule,
    DbrDrumSlot,
    DbrFeederSignal,
    Item,
    ProductionProduct,
    SyncLink,
)
from ..odata_client import OData1CClient
from ..odata_config import load_odata_config as _load_odata_config
from ..one_c_export_common import (
    clean_ref1c,
    create_odata_client,
    add_origin_marker,
    find_document_by_origin,
    origin_token,
    post_document_operational,
    post_export_entries,
)
from ..one_c_production_order_export import (
    PRODUCTION_ORDER_ENTITY,
    ProductionOrderExportEntry,
    ProductionOrderExportLine,
    _build_header_payload,
    _export_defaults,
    _materials_for_spec,
    _operations_for_spec,
    _workshop_warehouse_refs,
)
from ..workshop_resolution import (
    default_spec_ids_for_items,
    resolve_workshop_for_spec,
)
from .core.feeder import signal_identity
from . import planning_lineage

SOURCE_SYSTEM = "dbr"
SLOT_DOCTYPE = "drum_slot"
SIGNAL_DOCTYPE = "feeder_signal"


class MaterializeConflict(Exception):
    """A slot/signal is not in a state that can be materialized (HTTP 409).

    ``detail`` carries a human message; ``payload`` optionally carries structured
    context (e.g. the deficit_lines that block a launch).
    """

    def __init__(self, detail: str, payload: Optional[dict[str, Any]] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.payload = payload or {}


# ---------------------------------------------------------------------------
# sync_link helpers (source_system='dbr') — mirror one_c_export_common but with
# the DBR marker, so the shared PRODPLAN helpers stay untouched.
# ---------------------------------------------------------------------------


def _find_dbr_link(db: Session, *, source_doctype: str, source_id: int) -> Optional[SyncLink]:
    return (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == SOURCE_SYSTEM,
            SyncLink.source_doctype == source_doctype,
            SyncLink.source_id == int(source_id),
            SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
        )
        .one_or_none()
    )


def _upsert_dbr_link(
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
    existing = _find_dbr_link(db, source_doctype=source_doctype, source_id=source_id)
    synced_at = datetime.now(timezone.utc) if status == "success" else None
    if existing is None:
        db.add(
            SyncLink(
                source_system=SOURCE_SYSTEM,
                source_doctype=source_doctype,
                source_id=int(source_id),
                target_system="1C",
                target_entity=PRODUCTION_ORDER_ENTITY,
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


# ---------------------------------------------------------------------------
# Payload building — one ProductionOrderExportEntry per slot/signal.
# ---------------------------------------------------------------------------


def _make_entry(
    db: Session,
    *,
    source_id: int,
    number: str,
    item: Item,
    qty: float,
    product_destination_ref: Optional[str],
    reserve_ref: Optional[str],
    start_date: Optional[date],
    finish_date: Optional[date],
    source_run_id: int,
    ledger_generation_id: int,
    freeze_version: int,
) -> ProductionOrderExportEntry:
    ref1c = clean_ref1c(item.item_ref1c)
    if not ref1c:
        raise MaterializeConflict(
            f"item_id={item.item_id}: пустой item_ref1c — нельзя сопоставить с номенклатурой 1С"
        )
    spec_id = default_spec_ids_for_items(db, [int(item.item_id)]).get(int(item.item_id))
    spec_ref = None
    if spec_id:
        from ...models import Specification

        spec = db.query(Specification).filter(Specification.spec_id == int(spec_id)).first()
        spec_ref = clean_ref1c(spec.spec_ref1c) if spec else None
    unit_ref = clean_ref1c(item.unit) or None
    line = ProductionOrderExportLine(
        line_number=1,
        item_id=int(item.item_id),
        item_ref1c=ref1c,
        item_name=str(item.item_name or ""),
        item_article=str(item.item_article or ""),
        unit_ref1c=unit_ref,
        qty=float(qty),
        spec_ref1c=spec_ref or None,
        structural_unit_ref1c=product_destination_ref,
    )
    materials = _materials_for_spec(
        db, spec_id=spec_id, order_qty=float(qty), reserve_structural_unit_ref1c=reserve_ref
    )
    operations = _operations_for_spec(
        db,
        spec_id=spec_id,
        order_qty=float(qty),
        product_unit_ref1c=unit_ref,
        structural_unit_ref1c=product_destination_ref,
        product_link_key=1,
    )
    return ProductionOrderExportEntry(
        order_id=int(source_id),
        number=number,
        source_run_id=int(source_run_id),
        ledger_generation_id=int(ledger_generation_id),
        freeze_version=int(freeze_version),
        lines=[line],
        materials=materials,
        operations=operations,
        reserve_structural_unit_ref1c=reserve_ref,
        product_structural_unit_ref1c=product_destination_ref,
        planned_start_date=start_date,
        planned_finish_date=finish_date,
        document_date=start_date or finish_date,
    )


def _build_payload(
    entry: ProductionOrderExportEntry,
    source_doctype: str,
    *,
    durable_identity: Optional[str] = None,
) -> dict[str, Any]:
    config = _load_odata_config()
    defaults = _export_defaults(config)
    payload = _build_header_payload(entry, defaults)
    # Re-label the free-text comment so the 1С document is traceable back to the
    # DBR source rather than an MRP production_order.
    payload["Комментарий"] = (
        f"PRODPLAN source={source_doctype}/{entry.order_id}; dbr; number={entry.number}"
    )
    entry.origin_token = origin_token(
        f"dbr:{source_doctype}",
        durable_identity or {
            "lines": [
                {
                    "item_ref1c": line.item_ref1c,
                    "characteristic_ref1c": line.characteristic_ref1c or "",
                    "qty": float(line.qty),
                }
                for line in entry.lines
            ],
            "planned_start": str(entry.planned_start_date or ""),
            "planned_finish": str(entry.planned_finish_date or ""),
        },
    )
    payload["Комментарий"] = add_origin_marker(payload["Комментарий"], entry.origin_token)
    return payload


def _write_entry(
    db: Session,
    *,
    entry: ProductionOrderExportEntry,
    payload: dict[str, Any],
    source_doctype: str,
    stamp: Callable[[str, str], None],
    allow_production: bool,
) -> dict[str, Any]:
    config = _load_odata_config()
    client = create_odata_client(
        config, OData1CClient, allow_production=allow_production, require_demo_base=True
    )

    existing_doc = find_document_by_origin(
        client,
        entity=PRODUCTION_ORDER_ENTITY,
        token=str(entry.origin_token),
    )
    recovered_ref = clean_ref1c((existing_doc or {}).get("Ref_Key"))
    if recovered_ref and not clean_ref1c(entry.target_ref_key):
        entry.target_ref_key = recovered_ref
        _upsert_dbr_link(
            db,
            source_doctype=source_doctype,
            source_id=int(entry.order_id),
            target_number=str((existing_doc or {}).get("Number") or entry.number),
            payload_hash="recovered-by-origin",
            target_ref_key=recovered_ref,
            status="success",
            last_error=None,
        )
        stamp(
            recovered_ref,
            str((existing_doc or {}).get("Number") or entry.number),
        )
        db.commit()
        entry.status = "existing"
        return {
            "created": 0,
            "errored": 0,
            "target_ref_key": recovered_ref,
            "status": "existing",
            "error": None,
        }

    def _on_success(export_entry: ProductionOrderExportEntry, ref_key: str) -> None:
        post_document_operational(
            client, entity=PRODUCTION_ORDER_ENTITY, ref_key=ref_key, unpost_first=False
        )
        stamp(ref_key, export_entry.number)

    def _link(*, entry, payload_hash, target_ref_key, status, last_error):
        _upsert_dbr_link(
            db,
            source_doctype=source_doctype,
            source_id=int(entry.order_id),
            target_number=entry.number,
            payload_hash=payload_hash,
            target_ref_key=target_ref_key,
            status=status,
            last_error=last_error,
        )

    created, errored = post_export_entries(
        db,
        entries=[(entry, {"order_id": entry.order_id, "number": entry.number, "payload": payload})],
        client=client,
        target_entity=PRODUCTION_ORDER_ENTITY,
        missing_ref_error=f"1C did not return Ref_Key for the new {PRODUCTION_ORDER_ENTITY}",
        upsert_link=_link,
        on_success=_on_success,
        log_error=lambda e: f"[DBR materialize] {source_doctype}/{e.order_id} failed: {e.error}",
    )
    return {
        "created": created,
        "errored": errored,
        "target_ref_key": entry.target_ref_key,
        "status": entry.status,
        "error": entry.error,
    }


def _slot_number(slot: DbrDrumSlot) -> str:
    return f"DBRS{int(slot.id):07d}"


def _signal_number(signal: DbrFeederSignal) -> str:
    return f"DBRF{int(signal.id):07d}"


# ---------------------------------------------------------------------------
# release_slot
# ---------------------------------------------------------------------------


def release_slot(db: Session, slot_id: int, dry_run: bool = True) -> dict[str, Any]:
    """Release one green + pending drum slot → Document_ЗаказНаПроизводство in 1С.

    - dry_run=True (default): returns the payload preview, writes nothing.
    - dry_run=False: writes to 1С, stamps sync_link (source_system='dbr'),
      sets release_status='released' and records the 1С order ref/number.
    Red/yellow/unknown slots raise MaterializeConflict (HTTP 409): only green
    slots are releasable, no force override.
    """
    slot = db.get(DbrDrumSlot, slot_id)
    if slot is None:
        raise LookupError("slot not found")
    planning_lineage.require_row(db, slot, consumer="dbr_slot_release")

    existing = _find_dbr_link(db, source_doctype=SLOT_DOCTYPE, source_id=slot.id)
    already = existing is not None and existing.status == "success" and clean_ref1c(existing.target_ref_key)

    if not already:
        if slot.release_status == "completed":
            raise MaterializeConflict("плитка закрыта выпуском — релизовать нечего")
        if str(slot.kit_status or "") != "green":
            raise MaterializeConflict(
                f"слот не зелёный (гейт: {slot.kit_status or 'unknown'}) — "
                "релизовать можно только зелёные (green) плитки; обновите гейт"
            )
        if slot.release_status != "pending":
            raise MaterializeConflict(
                f"слот в статусе «{slot.release_status}» — релизовать можно только pending"
            )

    workshop_wh, production_wh = _workshop_warehouse_refs(db, slot.resource_id)
    product_destination_ref = production_wh or workshop_wh
    entry = _make_entry(
        db,
        source_id=int(slot.id),
        number=_slot_number(slot),
        item=slot.item,
        qty=float(slot.qty or 0),
        product_destination_ref=product_destination_ref,
        reserve_ref=workshop_wh,
        start_date=slot.slot_date,
        finish_date=slot.slot_date,
        source_run_id=int(slot.source_run_id),
        ledger_generation_id=int(slot.ledger_generation_id),
        freeze_version=int(slot.freeze_version),
    )
    if already:
        entry.target_ref_key = clean_ref1c(existing.target_ref_key)
    elif existing is not None and clean_ref1c(existing.target_ref_key):
        entry.target_ref_key = clean_ref1c(existing.target_ref_key)

    slot_identity = {
        "schedule_period": [
            str(slot.schedule.period_from),
            str(slot.schedule.period_to),
        ],
        "item_ref1c": clean_ref1c(slot.item.item_ref1c),
        "resource": slot.resource.resource_name if slot.resource else None,
        "planned_date": str(slot.planned_date),
        "position": slot.position,
        "qty": float(slot.qty or 0),
    }
    payload = _build_payload(
        entry,
        SLOT_DOCTYPE,
        durable_identity=str(slot_identity),
    )

    base = {
        "ok": True,
        "dry_run": bool(dry_run),
        "kind": "drum_slot",
        "slot_id": int(slot.id),
        "entity": PRODUCTION_ORDER_ENTITY,
        "number": entry.number,
        "payload": payload,
    }

    if already:
        return {
            **base,
            "created": False,
            "already_released": True,
            "release_status": slot.release_status,
            "one_c_order_ref": clean_ref1c(existing.target_ref_key),
            "note": "уже материализован в 1С (sync_link) — повторная отправка не выполнялась",
        }

    if dry_run:
        return {**base, "created": False, "note": "dry-run: предпросмотр payload, запись не выполнялась"}

    def _stamp(ref_key: str, number: str) -> None:
        slot.one_c_order_ref = ref_key
        slot.one_c_order_number = number
        slot.release_status = "released"

    outcome = _write_entry(
        db,
        entry=entry,
        payload=payload,
        source_doctype=SLOT_DOCTYPE,
        stamp=_stamp,
        allow_production=True,
    )
    return {
        **base,
        "created": outcome["created"] == 1,
        "release_status": slot.release_status,
        "one_c_order_ref": clean_ref1c(entry.target_ref_key) or None,
        "error": outcome["error"],
    }


# ---------------------------------------------------------------------------
# launch_signal
# ---------------------------------------------------------------------------


def _signal_can_launch(db: Session, signal: DbrFeederSignal) -> dict[str, Any]:
    """Material readiness note for one signal, taken from the live queue netting."""
    from . import feeder_material_service

    signals = feeder_material_service.live_queue(db)
    annotations = feeder_material_service.annotate_queue(db, signals, with_roots=False)["annotations"]
    return annotations.get(int(signal.id), {})


def launch_signal(
    db: Session,
    signal_id: int,
    dry_run: bool = True,
    allow_production: bool = False,
) -> dict[str, Any]:
    """Launch one Open + complete feeder signal → Document_ЗаказНаПроизводство in 1С.

    Gate: the signal must be Open, complete, and pass the material ``can_launch``
    verdict from feeder_material_service. A deficit raises MaterializeConflict
    (HTTP 409) carrying ``deficit_lines``. On a real write the signal moves to
    'Order Created' and records the 1С order ref/number.
    """
    signal = db.get(DbrFeederSignal, signal_id)
    if signal is None:
        raise LookupError("signal not found")
    planning_lineage.require_row(db, signal, consumer="dbr_signal_launch")

    existing = _find_dbr_link(db, source_doctype=SIGNAL_DOCTYPE, source_id=signal.id)
    already = existing is not None and existing.status == "success" and clean_ref1c(existing.target_ref_key)

    if not already:
        if signal.status != signal_identity.OPEN:
            raise MaterializeConflict(
                f"сигнал в статусе «{signal.status}» — запускать можно только Open"
            )
        if signal.is_incomplete:
            raise MaterializeConflict("сигнал неполный (data_quality) — запускать нельзя")
        if float(signal.suggested_qty or 0) <= 0:
            raise MaterializeConflict("рекомендуемое количество сигнала равно нулю")
        note = _signal_can_launch(db, signal)
        if not note.get("can_launch", False):
            raise MaterializeConflict(
                "материальный дефицит — запуск заблокирован",
                payload={
                    "material_status": note.get("material_status"),
                    "deficit_lines": note.get("deficit_lines", []),
                },
            )

    workshop_id = None
    spec_id = default_spec_ids_for_items(db, [int(signal.item_id)]).get(int(signal.item_id))
    if spec_id:
        workshop_id = resolve_workshop_for_spec(db, spec_id)
    workshop_wh, _production_wh = _workshop_warehouse_refs(db, workshop_id)
    # The produced part lands on its supermarket shelf (signal.warehouse_ref1c);
    # materials are reserved at the making workshop's WIP warehouse.
    product_destination_ref = clean_ref1c(signal.warehouse_ref1c) or workshop_wh
    entry = _make_entry(
        db,
        source_id=int(signal.id),
        number=_signal_number(signal),
        item=signal.item,
        qty=float(signal.suggested_qty or 0),
        product_destination_ref=product_destination_ref,
        reserve_ref=workshop_wh,
        start_date=signal.need_date,
        finish_date=signal.required_date or signal.need_date,
        source_run_id=int(signal.source_run_id),
        ledger_generation_id=int(signal.ledger_generation_id),
        freeze_version=int(signal.freeze_version),
    )
    if already or (existing is not None and clean_ref1c(existing.target_ref_key)):
        entry.target_ref_key = clean_ref1c(existing.target_ref_key)

    payload = _build_payload(
        entry,
        SIGNAL_DOCTYPE,
        durable_identity=str(signal.dedup_key),
    )

    base = {
        "ok": True,
        "dry_run": bool(dry_run),
        "kind": "feeder_signal",
        "signal_id": int(signal.id),
        "entity": PRODUCTION_ORDER_ENTITY,
        "number": entry.number,
        "payload": payload,
    }

    if already:
        return {
            **base,
            "created": False,
            "already_launched": True,
            "status": signal.status,
            "one_c_order_ref": clean_ref1c(existing.target_ref_key),
            "note": "уже материализован в 1С (sync_link) — повторная отправка не выполнялась",
        }

    if dry_run:
        return {**base, "created": False, "note": "dry-run: предпросмотр payload, запись не выполнялась"}

    def _stamp(ref_key: str, number: str) -> None:
        signal.one_c_order_ref = ref_key
        signal.one_c_order_number = number
        signal.status = signal_identity.ORDER_CREATED
        product = (
            db.query(ProductionProduct)
            .filter(ProductionProduct.source_dbr_signal_id == int(signal.id))
            .one_or_none()
        )
        if product is not None:
            product.order.order_ref1c = ref_key
            if number:
                product.order.order_number = number

    outcome = _write_entry(
        db,
        entry=entry,
        payload=payload,
        source_doctype=SIGNAL_DOCTYPE,
        stamp=_stamp,
        allow_production=allow_production,
    )
    return {
        **base,
        "created": outcome["created"] == 1,
        "status": signal.status,
        "one_c_order_ref": clean_ref1c(entry.target_ref_key) or None,
        "error": outcome["error"],
    }


# ---------------------------------------------------------------------------
# release_day — batch release of green + pending slots for one day
# ---------------------------------------------------------------------------


def release_day(
    db: Session, schedule_id: int, day: date, dry_run: bool = True
) -> dict[str, Any]:
    """Release every green + pending slot of ``day`` in one schedule.

    Partial failures never roll back the slots that already succeeded: each slot
    is materialized independently (post_export_entries commits per document,
    the same isolation prodflow's release_service gets from a per-slot
    savepoint). The report lists the outcome of every slot.
    """
    schedule = db.get(DbrDrumSchedule, schedule_id)
    if schedule is None:
        raise LookupError("schedule not found")

    slots = (
        db.query(DbrDrumSlot)
        .filter(
            DbrDrumSlot.schedule_id == int(schedule_id),
            DbrDrumSlot.slot_date == day,
            DbrDrumSlot.release_status == "pending",
            DbrDrumSlot.kit_status == "green",
        )
        .order_by(DbrDrumSlot.position, DbrDrumSlot.id)
        .all()
    )

    results: list[dict[str, Any]] = []
    released = errors = previews = 0
    for slot in slots:
        try:
            res = release_slot(db, int(slot.id), dry_run=dry_run)
            results.append(res)
            if dry_run:
                previews += 1
            elif res.get("created"):
                released += 1
            elif res.get("error"):
                errors += 1
        except MaterializeConflict as exc:
            # Should be rare (we pre-filtered green+pending), but keep the batch
            # alive and report the individual refusal.
            db.rollback()
            errors += 1
            results.append(
                {
                    "ok": False,
                    "dry_run": bool(dry_run),
                    "slot_id": int(slot.id),
                    "conflict": exc.detail,
                    **({"detail": exc.payload} if exc.payload else {}),
                }
            )
        except Exception as exc:  # noqa: BLE001 — isolate one slot's failure
            db.rollback()
            errors += 1
            results.append(
                {"ok": False, "dry_run": bool(dry_run), "slot_id": int(slot.id), "error": str(exc)}
            )

    return {
        "ok": errors == 0,
        "dry_run": bool(dry_run),
        "schedule_id": int(schedule_id),
        "day": str(day),
        "slots_total": len(slots),
        "released": released,
        "previews": previews,
        "errors": errors,
        "results": results,
    }
