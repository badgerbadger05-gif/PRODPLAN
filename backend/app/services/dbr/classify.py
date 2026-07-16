"""DBR kit-boundary classifier (аналог prodflow gate_service.classify_item).

Decides where a kit walk stops for each component (техдизайн Фазы 1 §4).
Boundaries in PRODPLAN terms:

- FASTENER — метиз: item's category name is in dbr_settings.fastener_categories
  → excluded from the kit (free-issue). Empty list ⇒ nobody is a fastener.
- RECURSE — фантом: a phantom grouping node → walk deeper. PRODPLAN has no
  phantom flag today, so this is opt-in via ItemMeta.is_phantom and defaults
  off (documented ambiguity); manufactured intermediates instead stop at a
  shelf below.
- W2 — заготовка-буфер мехцеха: the item is manufactured and the workshop that
  makes it (spec.production_kind → resource_production_kinds → resource) delivers
  to the Склад №2 warehouse (workshop_warehouse_bindings.production_warehouse_ref1c
  == settings.w2_warehouse_ref1c). Checked before purchase/№3/№4 — the mechshop
  blank is the priority decoupling point.
- W4 — закупное: replenishment_method is a purchase flow (is_purchase_replenishment).
- W3 — окрашенная деталь: manufactured item that has stock on the Склад №3
  warehouse (item_warehouse_stock row with qty>0) — v1 "по фактическим Bin".
- W4 (узел) — manufactured item without a №3 shelf → Склад №4 sub-assembly.
- UNDER_SCHEDULE — деталь без полки: leaf detail (no spec, not purchased) → kit
  with the "под график" mark, sourced from Склад №4.

The pure decision (classify_meta) is unit-tested on fixture ItemMeta; the
DB-backed build_classifier assembles ItemMeta from shared tables. Disputable
cases (missing shelf warehouse, unresolvable item) are collected as notes and
never raise — the kit build must not fall over on messy master data.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ...models import (
    DefaultSpecification,
    Item,
    ItemCategory,
    ItemWarehouseStock,
    ResourceProductionKind,
    Specification,
    WorkshopWarehouseBinding,
)
from ..replenishment import is_purchase_replenishment
from .core.drum import kit as kit_mod


@dataclass
class ItemMeta:
    """Everything the pure classifier needs about one component."""

    item_code: str
    is_fastener: bool = False
    is_purchase: bool = False
    is_w2_blank: bool = False
    has_w3_shelf: bool = False
    has_spec: bool = False
    is_phantom: bool = False


def classify_meta(
    meta: ItemMeta,
    w2_wh: str | None,
    w3_wh: str | None,
    w4_wh: str | None,
) -> tuple[str, str | None, str | None]:
    """Return (decision, source_warehouse | None, note | None).

    ``note`` is set only for disputable/degraded cases (a boundary that cannot
    resolve to a configured warehouse, so the component is dropped from the
    kit). Happy-path decisions carry no note.
    """
    if meta.is_fastener:
        return kit_mod.FASTENER, None, None
    if meta.is_phantom:
        return kit_mod.RECURSE, None, None
    if meta.is_w2_blank:
        if w2_wh:
            return kit_mod.W2, w2_wh, None
        return (
            kit_mod.FASTENER,
            None,
            f"{meta.item_code}: заготовка мехцеха, но склад №2 не настроен — исключена из кита",
        )
    if meta.is_purchase:
        if w4_wh:
            return kit_mod.W4, w4_wh, None
        return (
            kit_mod.FASTENER,
            None,
            f"{meta.item_code}: закупное, но склад №4 не настроен — исключено из кита",
        )
    if meta.has_spec:
        if meta.has_w3_shelf and w3_wh:
            return kit_mod.W3, w3_wh, None
        if w4_wh:
            return kit_mod.W4, w4_wh, None
        return (
            kit_mod.FASTENER,
            None,
            f"{meta.item_code}: сборка без полки, склад №4 не настроен — исключена из кита",
        )
    # leaf detail «без полки» — into the kit under schedule (W4 shelf)
    if w4_wh:
        return kit_mod.UNDER_SCHEDULE, w4_wh, None
    return (
        kit_mod.FASTENER,
        None,
        f"{meta.item_code}: деталь без полки, склад №4 не настроен — исключена из кита",
    )


def build_classifier(db: Session, settings):
    """Build (classify(item_code) -> (decision, warehouse), notes) from the DB.

    ``classify`` is the callback kit.build_kit expects. ``notes`` accumulates
    disputable cases encountered during the walk (deduplicate at the caller).
    """
    w2 = settings.w2_warehouse_ref1c
    w3 = settings.w3_warehouse_ref1c
    w4 = settings.w4_warehouse_ref1c
    fastener_names = set(settings.fastener_categories or [])

    items = {i.item_code: i for i in db.query(Item).all()}
    cat_name = {c.category_id: c.category_name for c in db.query(ItemCategory.category_id, ItemCategory.category_name).all()}

    default_spec: dict[int, int] = {}
    for ds in db.query(DefaultSpecification).order_by(DefaultSpecification.id.asc()).all():
        default_spec.setdefault(int(ds.item_id), int(ds.spec_id))
    spec_kind = {
        int(sid): kind for sid, kind in db.query(Specification.spec_id, Specification.production_kind_id).all()
    }

    # production_kind_id -> set of production output warehouses (via bindings)
    binding_by_resource = {
        int(b.workshop_id): b.production_warehouse_ref1c
        for b in db.query(WorkshopWarehouseBinding.workshop_id, WorkshopWarehouseBinding.production_warehouse_ref1c).all()
    }
    kind_prod_wh: dict[int, set[str]] = {}
    for rpk in db.query(ResourceProductionKind.resource_id, ResourceProductionKind.production_kind_id).all():
        wh = binding_by_resource.get(int(rpk.resource_id))
        if wh:
            kind_prod_wh.setdefault(int(rpk.production_kind_id), set()).add(wh)

    w3_items: set[int] = set()
    if w3:
        for (iid,) in (
            db.query(ItemWarehouseStock.item_id)
            .filter(ItemWarehouseStock.warehouse_ref1c == w3, ItemWarehouseStock.qty > 0)
            .all()
        ):
            w3_items.add(int(iid))

    def meta_for(code: str) -> ItemMeta:
        item = items.get(code)
        if item is None:
            return ItemMeta(code)
        iid = int(item.item_id)
        is_fastener = bool(item.category_id is not None and cat_name.get(item.category_id) in fastener_names)
        is_purchase = is_purchase_replenishment(item.replenishment_method)
        spec_id = default_spec.get(iid)
        has_spec = spec_id is not None
        kind = spec_kind.get(spec_id) if spec_id is not None else None
        is_w2 = bool(w2 and kind is not None and w2 in kind_prod_wh.get(kind, set()))
        has_w3 = iid in w3_items
        return ItemMeta(
            item_code=code,
            is_fastener=is_fastener,
            is_purchase=is_purchase,
            is_w2_blank=is_w2,
            has_w3_shelf=has_w3,
            has_spec=has_spec,
        )

    notes: list[str] = []
    cache: dict[str, tuple[str, str | None]] = {}

    def classify(code: str) -> tuple[str, str | None]:
        cached = cache.get(code)
        if cached is not None:
            return cached
        decision, warehouse, note = classify_meta(meta_for(code), w2, w3, w4)
        if note:
            notes.append(note)
        cache[code] = (decision, warehouse)
        return cache[code]

    return classify, notes
