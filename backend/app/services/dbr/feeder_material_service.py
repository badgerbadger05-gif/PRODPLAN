"""Material readiness of the mechshop queue (Фаза 3.1 — PRODPLAN port).

Port of prodflow ``feeder/material_service.py`` onto the shared PRODPLAN schema.
Ties three ready-made layers together:

- ``core.drum.kit.build_kit`` — the signal's spec trimmed to the first
  decoupling boundary (supermarket shelf / purchased / manufactured detail
  without a shelf);
- ``core.feeder.availability`` — cumulative single-pool netting down the queue;
- a stock snapshot ``selected − ignored`` built exactly like the existing
  adapters (item-oriented enterprise availability).

Every read is over shared tables only; nothing is written here and no 1С call
is made (module invariants). The heavy arithmetic lives in the pure core; this
adapter only does batch selects, kit-line classification and UI aggregates.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from sqlalchemy.orm import Session

from ...models import (
    DbrFeederSignal,
    DbrSettings,
    DbrSupermarketPosition,
    DefaultSpecification,
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    SpecComponent,
    StockWarehouse,
)
from ..replenishment import is_purchase_replenishment
from . import adapters, classify as classify_mod
from .core.drum.kit import build_kit
from .core.feeder import availability, roots as roots_mod, signal_identity


def _gross_stock(db: Session, item_ids: set[int]) -> dict[int, float]:
    """Enterprise stock per item_id: sum over selected-and-not-ignored warehouses.

    Same policy as ``adapters.stock_snapshot_by_code`` (is_selected minus
    ignored_warehouses), but item-oriented — the availability pool is per code,
    not per shelf (design §3, «доступность по предприятию»).
    """
    if not item_ids:
        return {}
    ignored = {r[0] for r in db.query(IgnoredWarehouse.warehouse_ref1c).all() if r[0]}
    wh_rows = db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.is_selected).all()
    has_settings = bool(wh_rows)
    selected = {ref for ref, sel in wh_rows if ref and bool(sel)}
    out: dict[int, float] = defaultdict(float)
    for iid, ref, qty in (
        db.query(
            ItemWarehouseStock.item_id,
            ItemWarehouseStock.warehouse_ref1c,
            ItemWarehouseStock.qty,
        )
        .filter(ItemWarehouseStock.item_id.in_(item_ids))
        .all()
    ):
        if ref in ignored:
            continue
        if has_settings and ref not in selected:
            continue
        out[int(iid)] += float(qty or 0)
    return dict(out)


def _kit_item_meta(
    db: Session, codes: list[str], code_to_id: dict[str, int]
) -> dict[str, dict[str, Any]]:
    """Per-kit-item metadata: buffer (active shelf), produced (make), names.

    ``buffer`` — the component has an active supermarket position (its own
    replenishment signal, so a chain child is NOT spawned — §4 п.5).
    ``produced`` — manufactured (has a default spec and is not purchase-supplied),
    which decides MAKE vs BUY for the deficit aggregate and the chain decision.
    """
    meta: dict[str, dict[str, Any]] = {}
    if not codes:
        return meta
    ids = [code_to_id[c] for c in codes if c in code_to_id]
    if not ids:
        return meta

    buffered_ids: set[int] = {
        int(iid)
        for (iid,) in db.query(DbrSupermarketPosition.item_id)
        .filter(
            DbrSupermarketPosition.item_id.in_(ids),
            DbrSupermarketPosition.is_active.is_(True),
        )
        .all()
    }
    has_spec: set[int] = {
        int(iid)
        for (iid,) in db.query(DefaultSpecification.item_id)
        .filter(DefaultSpecification.item_id.in_(ids))
        .all()
    }
    for item in db.query(Item).filter(Item.item_id.in_(ids)).all():
        iid = int(item.item_id)
        purchase = is_purchase_replenishment(item.replenishment_method)
        meta[item.item_code] = {
            "buffer": iid in buffered_ids,
            "produced": (iid in has_spec) and not purchase,
            "item_name": item.item_name or "",
            "article": item.item_article or "",
        }
    for code in codes:
        meta.setdefault(
            code,
            {"buffer": False, "produced": False, "item_name": "", "article": ""},
        )
    return meta


def _need_kind_level(buffered: bool, produced: bool) -> tuple[str, str]:
    """Boundary kind (make/buy) and a human level mark for one kit line."""
    kind = availability.MAKE if produced else availability.BUY
    if buffered:
        return kind, "полка"
    if produced:
        return kind, "без полки → цепочка"
    return kind, "закупной"


def _parent_map(db: Session, id_to_code: dict[int, str]) -> dict[str, set[str]]:
    """component_code -> codes of items whose default spec contains it."""
    default_spec: dict[int, int] = {}
    for ds in db.query(DefaultSpecification).order_by(DefaultSpecification.id.asc()).all():
        default_spec.setdefault(int(ds.item_id), int(ds.spec_id))
    comps_by_spec: dict[int, list[int]] = defaultdict(list)
    for spec_id, comp_id in db.query(SpecComponent.spec_id, SpecComponent.item_id).all():
        comps_by_spec[int(spec_id)].append(int(comp_id))
    parents: dict[str, set[str]] = defaultdict(set)
    for item_id, spec_id in default_spec.items():
        parent_code = id_to_code.get(item_id)
        if parent_code is None:
            continue
        for comp_id in comps_by_spec.get(spec_id, ()):
            child_code = id_to_code.get(comp_id)
            if child_code is not None and child_code != parent_code:
                parents[child_code].add(parent_code)
    return dict(parents)


def annotate_queue(
    db: Session,
    signals: list[DbrFeederSignal],
    *,
    with_roots: bool = True,
) -> dict[str, Any]:
    """Compute material readiness for an already ordered live queue.

    ``signals`` — live feeder signals in queue order (kit-force → priority → id).
    Only ``Open`` signals join the cumulative netting (design §3 п.2); others get
    no status. Returns per-signal annotations keyed by signal id, the raw
    ``SignalKit`` verdicts (for the chain generator) and the deficit aggregate.

    Raises ``ValueError`` when DBR settings/warehouse-roles are not configured —
    the caller decides whether to degrade gracefully.
    """
    settings = db.get(DbrSettings, 1)
    if settings is None:
        raise ValueError("настройки DBR не созданы")
    # build_classifier raises ValueError if the warehouse roles are unset.
    classify, _notes = classify_mod.build_classifier(db, settings)
    components_of = adapters.build_components_provider(db)
    id_to_code, code_to_id = adapters.item_code_maps(db)

    open_signals = [s for s in signals if s.status == signal_identity.OPEN]

    kit_cache: dict[str, Optional[list]] = {}
    raw_by_id: dict[int, list] = {}
    failed: set[int] = set()
    kit_codes: set[str] = set()
    for sig in open_signals:
        code = id_to_code.get(int(sig.item_id))
        if not code:
            failed.add(int(sig.id))
            continue
        if code not in kit_cache:
            try:
                kit_cache[code] = build_kit(code, components_of, classify)
            except Exception:
                # A broken/cyclic BOM must not sink the whole queue: that signal
                # is dropped from netting and left without a material status.
                kit_cache[code] = None
        lines = kit_cache[code]
        if lines is None:
            failed.add(int(sig.id))
            continue
        raw_by_id[int(sig.id)] = lines
        for ln in lines:
            kit_codes.add(ln.item)

    meta = _kit_item_meta(db, sorted(kit_codes), code_to_id)
    gross_by_id = _gross_stock(db, {code_to_id[c] for c in kit_codes if c in code_to_id})
    gross = {c: gross_by_id.get(code_to_id[c], 0.0) for c in kit_codes if c in code_to_id}

    netted = [s for s in open_signals if int(s.id) not in failed]
    needs_by_id: dict[int, list[availability.KitLineNeed]] = {}
    for sig in netted:
        qty = float(sig.suggested_qty or 0.0)
        needs: list[availability.KitLineNeed] = []
        for ln in raw_by_id[int(sig.id)]:
            m = meta.get(ln.item, {})
            buffered = bool(m.get("buffer"))
            produced = bool(m.get("produced"))
            kind, level = _need_kind_level(buffered, produced)
            needs.append(
                availability.KitLineNeed(
                    item=ln.item,
                    need=round(ln.qty_per_unit * qty, 4),
                    kind=kind,
                    level=level,
                    buffered=buffered,
                )
            )
        needs_by_id[int(sig.id)] = needs

    verdicts = availability.evaluate_queue(
        [needs_by_id[int(s.id)] for s in netted], gross
    )
    kits = {int(s.id): v for s, v in zip(netted, verdicts, strict=True)}

    roots_by_code: dict[str, list[dict[str, Any]]] = {}
    if with_roots:
        parents = _parent_map(db, id_to_code)
        memo: dict[str, tuple[str, ...]] = {}
        wanted = {id_to_code.get(int(s.item_id)) for s in netted}
        wanted.discard(None)
        root_codes: set[str] = set()
        resolved: dict[str, tuple[str, ...]] = {}
        for code in sorted(c for c in wanted if c):
            rs = tuple(
                r
                for r in roots_mod.resolve_roots(code, lambda c: parents.get(c, ()), memo)
                if r != code
            )
            resolved[code] = rs
            root_codes.update(rs)
        root_meta: dict[str, dict[str, Any]] = {}
        if root_codes:
            for item in db.query(Item).filter(Item.item_code.in_(root_codes)).all():
                root_meta[item.item_code] = {
                    "item": item.item_code,
                    "item_name": item.item_name or item.item_code,
                    "article": item.item_article or "",
                }
        roots_by_code = {
            code: [
                root_meta.get(r, {"item": r, "item_name": r, "article": ""}) for r in rs
            ]
            for code, rs in resolved.items()
        }

    annotations: dict[int, dict[str, Any]] = {}
    deficit_acc: dict[str, dict[str, Any]] = {}
    for sig, verdict in zip(netted, verdicts, strict=True):
        code = id_to_code.get(int(sig.item_id))
        kit_lines = [
            {
                "item": ln.item,
                "item_name": meta.get(ln.item, {}).get("item_name") or ln.item,
                "article": meta.get(ln.item, {}).get("article") or "",
                "need": round(ln.need, 4),
                "have": round(ln.have, 4),
                "gross": round(ln.gross, 4),
                "kind": ln.kind,
                "level": ln.level,
                "cls": ln.cls,
                "buffered": ln.buffered,
            }
            for ln in verdict.lines
        ]
        annotations[int(sig.id)] = {
            "material_status": verdict.status,
            "kit_cls": verdict.cls,
            "can_launch": verdict.can_launch,
            "kit_lines": kit_lines,
            "deficit_lines": [line for line in kit_lines if line["cls"] in ("no", "part")],
            "root_items": roots_by_code.get(code or "", []),
        }
        for ln in verdict.lines:
            if ln.cls not in ("no", "part"):
                continue
            acc = deficit_acc.get(ln.item)
            if acc is None:
                acc = {
                    "item": ln.item,
                    "item_name": meta.get(ln.item, {}).get("item_name") or ln.item,
                    "article": meta.get(ln.item, {}).get("article") or "",
                    "source": "make" if ln.kind == availability.MAKE else "buy",
                    "gross": round(ln.gross, 4),
                    "need_sum": 0.0,
                    "blocks_signals": 0,
                    "nearest_due": None,
                }
                deficit_acc[ln.item] = acc
            acc["need_sum"] += ln.need
            acc["blocks_signals"] += 1
            due = sig.need_date
            if due and (acc["nearest_due"] is None or str(due) < acc["nearest_due"]):
                acc["nearest_due"] = str(due)

    deficits = []
    for acc in deficit_acc.values():
        short = round(max(acc["need_sum"] - acc["gross"], 0.0), 4)
        if short <= availability.EPS:
            continue
        deficits.append(
            {
                "item": acc["item"],
                "item_name": acc["item_name"],
                "article": acc["article"],
                "source": acc["source"],
                "short_qty": short,
                "need_sum": round(acc["need_sum"], 4),
                "gross": acc["gross"],
                "blocks_signals": acc["blocks_signals"],
                "nearest_due": acc["nearest_due"],
            }
        )
    deficits.sort(key=lambda d: (d["nearest_due"] or "9999", -d["blocks_signals"]))

    return {
        "annotations": annotations,
        "kits": kits,
        "deficits": deficits,
        "deficit_materials": len(deficits),
        "netted": len(netted),
        "failed": len(failed),
    }


def live_queue(db: Session) -> list[DbrFeederSignal]:
    """Live feeder signals in queue order (kit-force → priority → id)."""
    return (
        db.query(DbrFeederSignal)
        .filter(DbrFeederSignal.status == signal_identity.OPEN)
        .order_by(
            DbrFeederSignal.kit_force.desc(),
            DbrFeederSignal.priority.desc(),
            DbrFeederSignal.id,
        )
        .all()
    )


def get_deficits(db: Session) -> dict[str, Any]:
    """Aggregate mechshop-queue material deficits (design §5). Read-only."""
    signals = live_queue(db)
    aggregate = annotate_queue(db, signals, with_roots=False)
    return {
        "deficits": aggregate["deficits"],
        "kpis": {
            "deficit_materials": aggregate["deficit_materials"],
            "queue_open": aggregate["netted"],
            "stock_source": "selected − ignored",
        },
    }
