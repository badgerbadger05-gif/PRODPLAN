"""Balance-snapshot normalization and the planning warehouse contour.

The 1С ``/Balance`` pull (``get_stock_from_1c_odata`` →
``convert_1c_stock_to_records``) returns rows keyed by
(Номенклатура, СтруктурнаяЕдиница, Организация) — WITHOUT Характеристика.
:func:`build_balance_snapshot` normalizes those rows onto the ledger's
AGGREGATE key ``LedgerKey(item_id, '', organization_ref, warehouse_ref1c)``:
rows are summed per key and a row whose item cannot be resolved locally is
dropped (``strict=True`` raises instead, for the physical-refresh callers that
must not silently lose stock).

Widening the Balance Dimensions with Характеристика was rejected without
verified live-1С evidence. Consumers therefore compare on the char=''
aggregate.

The physical truth is owned by the ``physical_refresh_*`` lifecycle
(:mod:`physical_refresh_orchestrator`, :mod:`opening_balance_reconcile`); this
module only shapes their input and exposes the planning warehouse contour. It
performs no OData write (INV-1way / INV-no-write) and no ledger mutation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy.orm import Session

from app import models
from .physical import EPS, LedgerKey, _dec


class BalanceSnapshotItemResolutionError(ValueError):
    """A non-zero 1C balance references nomenclature absent from PRODPLAN."""

    def __init__(self, identity: str):
        self.identity = str(identity)
        super().__init__(
            f"Balance row item cannot be resolved locally: {self.identity}"
        )


# ---------------------------------------------------------------------------
# Balance snapshot → ledger keys
# ---------------------------------------------------------------------------


def _resolve_item_maps(session: Session) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Return ({item_ref1c → item_id}, {norm_code → item_id}) for balance rows.

    Ref-first resolution matches how the puller keys bins (Item.item_ref1c), so a
    balance row and its bin land on the SAME item_id / ledger key.
    """
    from ..odata_stock_sync import _norm_code  # local import (avoid cycle)

    by_ref: Dict[str, int] = {}
    by_code: Dict[str, int] = {}
    for item_id, code, ref in session.query(
        models.Item.item_id, models.Item.item_code, models.Item.item_ref1c
    ).all():
        iid = int(item_id)
        r = str(ref or "").strip()
        if r:
            by_ref[r] = iid
        norm = _norm_code(str(code or ""))
        if norm:
            by_code.setdefault(norm, iid)
    return by_ref, by_code


def build_balance_snapshot(
    session: Session,
    balance_rows: Sequence[Mapping[str, Any]],
    *,
    strict: bool = False,
) -> Dict[LedgerKey, Decimal]:
    """Normalize converted Balance rows → ``{LedgerKey(char=''): qty}``.

    ``balance_rows`` is the ``get_stock_from_1c_odata`` shape ({code, ref,
    organization_ref, warehouse_ref, qty, ...}) — aggregate per (item, org,
    warehouse), no characteristic dimension (see module docstring). Rows are
    summed per ledger key; a row whose item cannot be resolved is dropped
    (``strict=True`` raises instead when its qty is non-zero).
    """
    from ..odata_stock_sync import _norm_code

    by_ref, by_code = _resolve_item_maps(session)
    snapshot: Dict[LedgerKey, Decimal] = {}
    for row in balance_rows or []:
        wh = str(row.get("warehouse_ref") or "").strip()
        ref = str(row.get("ref") or "").strip()
        item_id: Optional[int] = by_ref.get(ref) if ref else None
        if item_id is None:
            norm = _norm_code(str(row.get("code") or ""))
            item_id = by_code.get(norm) if norm else None
        if item_id is None:
            if strict and abs(_dec(row.get("qty") or 0)) > EPS:
                identity = ref or str(row.get("code") or "").strip() or "<missing>"
                raise BalanceSnapshotItemResolutionError(identity)
            continue
        org = str(row.get("organization_ref") or "").strip()
        key = LedgerKey(int(item_id), "", org, wh)
        snapshot[key] = snapshot.get(key, Decimal("0")) + _dec(row.get("qty") or 0)
    return snapshot


# ---------------------------------------------------------------------------
# planning warehouse contour — read-only
# ---------------------------------------------------------------------------


def contour_warehouse_refs(session: Session) -> Set[str]:
    """Refs of warehouses INSIDE the planning contour: selected, NOT ignored,
    NOT finished_goods.

    Used to classify a ``ПеремещениеЗапасов``: a ``transfer_out`` whose paired
    ``transfer_in`` lands on one of THESE warehouses is an INTERNAL pool move
    (the detail never left the contour) and must NOT realize a consume reserve.
    A transfer leaving the contour (workshop / external / ГП) does.

    Returns a POSITIVE set: only warehouses we can confirm are in-contour. When
    no warehouse settings exist at all the set is empty — the internal-move
    suppression then never fires and ``transfer_out`` keeps its legacy
    realize-always behavior (conservative: suppress only on a proven contour
    destination). ГП-склады are excluded even when also flagged selected.
    """
    ignored_refs = {
        str(r[0]) for r in session.query(models.IgnoredWarehouse.warehouse_ref1c).all()
        if r and r[0]
    }
    rows = session.query(
        models.StockWarehouse.warehouse_ref1c,
        models.StockWarehouse.is_selected,
        models.StockWarehouse.is_finished_goods,
    ).all()
    return {
        str(ref)
        for ref, sel, fg in rows
        if ref and bool(sel) and not bool(fg) and str(ref) not in ignored_refs
    }
