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

from ...models import DbrFeederSignal, DbrSettings
from . import adapters, feeder_material_service
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

    return {
        "created": created,
        "updated": updated,
        "reopened": reopened,
        "revoked": revoked,
        "no_warehouse": no_warehouse,
        "passes": passes,
    }
