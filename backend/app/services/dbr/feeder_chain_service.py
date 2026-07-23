"""Child «Цепочка» signals — chain explosion of the mechshop queue (Фаза 3.2).

Port of prodflow ``feeder/chain_service.py`` onto PRODPLAN.

A signal's kit deficit is pushed one level down: a *manufactured* component
*without a shelf* and *without stock* spawns a child «Цепочка» signal pegged to
its parent (``parent_signal_id``), inheriting the parent's priority — the same
buffer pulls the chain. A shelf component spawns nothing (its own replenishment
signal already carries that demand — §4 п.5, double-count guard); a purchased
component belongs to the purchasing loop, not the mechshop queue.

Decision rules live in the pure core (``core.feeder.chain``); here only batch
selects, the receiving warehouse and the signal lifecycle. Convergence: one pass
grows one level; several passes drive the chain down to the blank (each pass
sees the previous pass's children). A pass that creates nothing means the chain
converged. Depth is capped by ``chain.MAX_CHAIN_DEPTH``.

Switch: ``dbr_settings.feeder_chain_enabled`` (OFF by default — pegging spawns a
signal per parent, so the scale is measured with the preview first).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models import DbrFeederSignal, DbrSettings, DbrSupermarketPosition
from . import adapters, feeder_material_service
from .journal_bridge import sync_journal_rows
from .processing_trip_manifest import _assembly_component
from .core.feeder import chain, signal_identity

# Distinct advisory-lock key from the replenishment refresh ("DBRSIGNC").
_CHAIN_LOCK = 0x4442525349474E43

_REVOKE_PARENT_CLOSED = "Родительский сигнал закрыт или запущен — заготовка не нужна"
_REVOKE_COVERED = "Дефицит родителя закрыт — заготовка не нужна"


def _desired_children(
    signals: list[DbrFeederSignal],
    kits: dict[int, Any],
    id_to_code: dict[int, str],
) -> dict[tuple[int, str], tuple[DbrFeederSignal, chain.ChainDemand]]:
    """(parent_id, component_code) -> (parent signal, child demand) for now."""
    by_id = {int(s.id): s for s in signals}
    desired: dict[tuple[int, str], tuple[DbrFeederSignal, chain.ChainDemand]] = {}
    for sid, kit in kits.items():
        parent = by_id.get(int(sid))
        if parent is None:
            continue
        if int(parent.chain_depth or 0) >= chain.MAX_CHAIN_DEPTH:
            continue  # depth cap — protect against pathological BOM trees
        parent_code = id_to_code.get(int(parent.item_id))
        for demand in chain.plan_children(kit.lines):
            if demand.item == parent_code:
                continue  # self-reference: a detail is not its own blank
            desired[(int(parent.id), demand.item)] = (parent, demand)
    return desired


def _processing_contract_filter(
    db: Session,
    signals: list[DbrFeederSignal],
    desired: dict[tuple[int, str], tuple[DbrFeederSignal, chain.ChainDemand]],
) -> tuple[
    dict[tuple[int, str], tuple[DbrFeederSignal, chain.ChainDemand]],
    dict[int, list[str]],
]:
    """Keep only the single tolling blank allowed by the processing contract."""
    position_ids = {
        int(signal.supermarket_position_id)
        for signal in signals
        if signal.supermarket_position_id is not None
    }
    processing_position_ids = {
        int(row.id)
        for row in db.query(DbrSupermarketPosition)
        .filter(
            DbrSupermarketPosition.id.in_(position_ids or [-1]),
            DbrSupermarketPosition.supply_type == "processing",
        )
        .all()
    }
    allowed_by_parent: dict[int, str] = {}
    invalid: dict[int, list[str]] = {}
    for signal in signals:
        if signal.supermarket_position_id is None or int(
            signal.supermarket_position_id
        ) not in processing_position_ids:
            continue
        bare, _ratio, reasons = _assembly_component(db, int(signal.item_id))
        if reasons or bare is None:
            invalid[int(signal.id)] = reasons or ["assembly_component_unresolved"]
        else:
            allowed_by_parent[int(signal.id)] = str(bare.item_code)

    filtered = {
        key: value
        for key, value in desired.items()
        if key[0] not in invalid
        and (
            key[0] not in allowed_by_parent
            or key[1] == allowed_by_parent[key[0]]
        )
    }
    return filtered, invalid


def preview_chain_signals(db: Session) -> dict[str, Any]:
    """Dry-run: size the first chain level without creating anything (read-only).

    Pegging spawns a child on every parent, so before enabling on a live base the
    order of magnitude matters: how many first-level children appear, how many
    distinct blanks, and which blanks pull the most parents. Deeper levels are
    invisible to a dry-run (their parents are not-yet-created level-1 children).
    """
    settings = db.get(DbrSettings, 1)
    enabled = bool(settings.feeder_chain_enabled) if settings is not None else False
    signals = feeder_material_service.live_queue(db)
    aggregate = feeder_material_service.annotate_queue(db, signals, with_roots=False)
    id_to_code, _code_to_id = adapters.item_code_maps(db)
    desired = _desired_children(signals, aggregate["kits"], id_to_code)
    desired, _invalid_contracts = _processing_contract_filter(db, signals, desired)

    parents_by_item: dict[str, int] = {}
    qty_by_item: dict[str, float] = {}
    for (_parent_id, item_code), (_parent, demand) in desired.items():
        parents_by_item[item_code] = parents_by_item.get(item_code, 0) + 1
        qty_by_item[item_code] = qty_by_item.get(item_code, 0.0) + demand.qty

    top = sorted(parents_by_item.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    return {
        "enabled": enabled,
        "open_signals": len(signals),
        "level1_children": len(desired),
        "distinct_items": len(parents_by_item),
        "top_items": [
            {"item": code, "parents": count, "qty_sum": round(qty_by_item[code], 4)}
            for code, count in top
        ],
    }


def preview_processing_chain_signals(db: Session) -> dict[str, Any]:
    """Dry-run desired children for processing-buffer parent signals only.

    This is deliberately read-only and shares ``_desired_children`` with the
    common chain refresh, preventing a second set of BOM/coverage rules.
    """
    settings = db.get(DbrSettings, 1)
    signals = (
        db.query(DbrFeederSignal)
        .join(
            DbrSupermarketPosition,
            DbrSupermarketPosition.id == DbrFeederSignal.supermarket_position_id,
        )
        .filter(
            DbrFeederSignal.status == signal_identity.OPEN,
            DbrSupermarketPosition.supply_type == "processing",
            DbrSupermarketPosition.is_active.is_(True),
        )
        .order_by(
            DbrFeederSignal.kit_force.desc(),
            DbrFeederSignal.priority.desc(),
            DbrFeederSignal.id,
        )
        .all()
    )
    aggregate = feeder_material_service.annotate_queue(db, signals, with_roots=False)
    id_to_code, code_to_id = adapters.item_code_maps(db)
    desired = _desired_children(signals, aggregate["kits"], id_to_code)
    desired, invalid_contracts = _processing_contract_filter(db, signals, desired)
    fallback_wh = settings.w2_warehouse_ref1c if settings is not None else None

    children: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    desired_parent_ids: set[int] = set()
    for (parent_id, item_code), (parent, demand) in sorted(desired.items()):
        desired_parent_ids.add(parent_id)
        reasons: list[str] = []
        if item_code not in code_to_id:
            reasons.append("component_item_not_found")
        if not (fallback_wh or parent.warehouse_ref1c):
            reasons.append("receiving_warehouse_missing")
        row = {
            "parent_signal_id": parent_id,
            "parent_item": id_to_code.get(int(parent.item_id)),
            "component_item": item_code,
            "suggested_qty": round(float(demand.qty), 4),
            "shortage_qty": round(float(demand.shortfall), 4),
            "warehouse_ref1c": fallback_wh or parent.warehouse_ref1c,
            "unresolved_reasons": reasons,
        }
        children.append(row)
        if reasons:
            unresolved.append(row)

    for signal in signals:
        contract_reasons = invalid_contracts.get(int(signal.id))
        if contract_reasons:
            unresolved.append(
                {
                    "parent_signal_id": int(signal.id),
                    "parent_item": id_to_code.get(int(signal.item_id)),
                    "component_item": None,
                    "suggested_qty": None,
                    "shortage_qty": None,
                    "warehouse_ref1c": fallback_wh or signal.warehouse_ref1c,
                    "unresolved_reasons": contract_reasons,
                }
            )

    netted_ids = set(aggregate["kits"])
    for signal in signals:
        if int(signal.id) not in netted_ids and int(signal.id) not in invalid_contracts:
            unresolved.append(
                {
                    "parent_signal_id": int(signal.id),
                    "parent_item": id_to_code.get(int(signal.item_id)),
                    "component_item": None,
                    "suggested_qty": None,
                    "shortage_qty": None,
                    "warehouse_ref1c": fallback_wh or signal.warehouse_ref1c,
                    "unresolved_reasons": ["parent_bom_or_item_unresolved"],
                }
            )

    return {
        "read_only": True,
        "processing_open_signals": len(signals),
        "netted_signals": int(aggregate["netted"]),
        "desired_children": len(children),
        "distinct_components": len({row["component_item"] for row in children}),
        "parents_with_children": len(desired_parent_ids),
        "children": children,
        "unresolved": unresolved,
        "unresolved_count": len(unresolved),
    }


def refresh_chain_signals(db: Session, max_passes: int = 3) -> dict[str, Any]:
    """Spawn/update/revoke child «Цепочка» signals (§4). Writes only Цепочка rows.

    Each pass: rebuild the queue → re-net → compare desired children with the
    live ones. Dedup is one live signal per (parent, component). Reopens a
    previously cancelled row instead of colliding on its unique dedup_key.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHAIN_LOCK})

    settings = db.get(DbrSettings, 1)
    if settings is None or not settings.feeder_chain_enabled:
        return {
            "disabled": True,
            "created": 0,
            "updated": 0,
            "reopened": 0,
            "revoked": 0,
            "no_warehouse": 0,
            "passes": 0,
        }

    fallback_wh = settings.w2_warehouse_ref1c
    created = updated = reopened = revoked = no_warehouse = 0
    passes = 0

    for _ in range(max(1, int(max_passes))):
        passes += 1
        signals = feeder_material_service.live_queue(db)
        aggregate = feeder_material_service.annotate_queue(db, signals, with_roots=False)
        kits = aggregate["kits"]
        id_to_code, code_to_id = adapters.item_code_maps(db)
        open_ids = {int(s.id) for s in signals}
        netted_parent_ids = set(kits.keys())
        desired = _desired_children(signals, kits, id_to_code)
        desired, _invalid_contracts = _processing_contract_filter(db, signals, desired)

        existing: dict[str, DbrFeederSignal] = {
            row.dedup_key: row
            for row in db.query(DbrFeederSignal)
            .filter(DbrFeederSignal.signal_type == "Цепочка")
            .all()
        }

        now = datetime.now()
        desired_keys: set[str] = set()
        # dedup_key -> parent id, so revocation can resolve the parent cheaply.
        parent_of_key: dict[str, int] = {}
        pass_created = 0
        for (parent_id, item_code), (parent, demand) in desired.items():
            item_id = code_to_id.get(item_code)
            if item_id is None:
                continue
            warehouse = fallback_wh or parent.warehouse_ref1c
            if not warehouse:
                no_warehouse += 1
                continue
            dedup = signal_identity.build_dedup_key(
                "Цепочка",
                parent_signal=str(parent.id),
                item=item_code,
                warehouse=warehouse,
            )
            desired_keys.add(dedup)
            parent_of_key[dedup] = int(parent.id)
            priority = chain.inherited_priority(float(parent.priority or 0))
            depth = int(parent.chain_depth or 0) + 1

            row = existing.get(dedup)
            if row is None:
                row = DbrFeederSignal(
                    dedup_key=dedup,
                    signal_type="Цепочка",
                    supermarket_position_id=None,
                    item_id=item_id,
                    warehouse_ref1c=warehouse,
                    parent_signal_id=int(parent.id),
                    chain_depth=depth,
                )
                db.add(row)
                existing[dedup] = row
                created += 1
                pass_created += 1
            elif row.status != signal_identity.OPEN:
                reopened += 1
            elif round(float(row.suggested_qty or 0), 4) != round(demand.qty, 4) or round(
                float(row.priority or 0), 4
            ) != priority:
                updated += 1

            row.status = signal_identity.OPEN
            row.cancelled_at = None
            row.suggested_qty = demand.qty
            row.priority = priority
            row.parent_signal_id = int(parent.id)
            row.chain_depth = depth
            row.need_date = parent.need_date
            row.required_date = parent.required_date
            row.raw_shortage_qty = demand.shortfall
            row.source_schedule_id = parent.source_schedule_id
            row.is_incomplete = False
            row.reason_json = {
                "generator": "feeder_chain",
                "parent_signal_id": int(parent.id),
                "chain_depth": depth,
                "shortfall": demand.shortfall,
            }
            row.refreshed_at = now
        db.flush()  # the next pass must see this pass's children

        # Revoke: parent closed/launched, or the parent's deficit is now covered.
        # A parent that is alive but fell out of netting (broken BOM) is NOT
        # touched — otherwise its children would be revoked on every pass.
        for dedup, row in list(existing.items()):
            if row.status != signal_identity.OPEN:
                continue
            if dedup in desired_keys:
                continue
            parent_id = row.parent_signal_id
            parent = db.get(DbrFeederSignal, parent_id) if parent_id else None
            if parent is None or parent.status != signal_identity.OPEN or int(parent.id) not in open_ids:
                reason = _REVOKE_PARENT_CLOSED
            elif int(parent.id) in netted_parent_ids:
                reason = _REVOKE_COVERED
            else:
                continue  # parent alive but not netted (broken BOM) — keep
            row.status = signal_identity.CANCELLED
            row.suggested_qty = 0
            row.cancelled_at = now
            row.refreshed_at = now
            row.reason_json = {"generator": "feeder_chain", "missing_reasons": [reason]}
            revoked += 1
        db.flush()

        if pass_created == 0:
            break  # no new links appeared — the chain converged

    journal_bridge = sync_journal_rows(db)
    return {
        "created": created,
        "updated": updated,
        "reopened": reopened,
        "revoked": revoked,
        "no_warehouse": no_warehouse,
        "passes": passes,
        "journal_bridge": journal_bridge,
    }
