"""Balance-snapshot normalization + the planning warehouse contour.

The Balance-reconcile *engine* (debounce → discovery → adjustment-SLE) was
retired: physical truth is owned by the physical_refresh lifecycle
(physical_refresh_orchestrator / opening_balance_reconcile), and its only
entry point was an unreachable opt-in branch of the legacy stock sweep. What
remains of :mod:`app.services.item_ledger.reconcile` is input shaping:

* build_balance_snapshot aligns converted rows on the full key (org included)
  and sums per aggregate key; unresolvable items are dropped (or raise under
  ``strict``);
* contour_warehouse_refs reports the selected / non-ignored / non-ГП contour.
"""

import pytest

from app import models
from app.services.item_ledger import (
    LedgerKey,
    build_balance_snapshot,
    contour_warehouse_refs,
)


def _item(db, code, ref, name=None, stock_qty=0.0):
    it = models.Item(item_code=code, item_name=name or code, item_ref1c=ref, stock_qty=stock_qty)
    db.add(it)
    db.flush()
    return it


# ---------------------------------------------------------------------------
# snapshot builder — org alignment (full key)
# ---------------------------------------------------------------------------


def test_build_balance_snapshot_aligns_on_full_key_with_org(db_session):
    it = _item(db_session, "P1", "ref-1")
    db_session.add(models.StockWarehouse(warehouse_ref1c="wh-1", warehouse_name="WH1"))
    db_session.flush()

    rows = [
        {"code": "P1", "ref": "ref-1", "organization_ref": "ORG1", "warehouse_ref": "wh-1", "qty": 4.0},
        {"code": "P1", "ref": "ref-1", "organization_ref": "ORG1", "warehouse_ref": "wh-1", "qty": 1.5},
        {"code": "??", "ref": "unknown-ref", "organization_ref": "ORG1", "warehouse_ref": "wh-1", "qty": 9.0},
    ]
    snap = build_balance_snapshot(db_session, rows)

    assert snap == {LedgerKey(it.item_id, "", "ORG1", "wh-1"): 5.5}  # summed; unknown dropped


def test_build_balance_snapshot_strict_rejects_unresolvable_nonzero_row(db_session):
    _item(db_session, "P1", "ref-1")
    db_session.flush()

    rows = [{"code": "??", "ref": "unknown-ref", "organization_ref": "", "warehouse_ref": "wh-1", "qty": 9.0}]
    with pytest.raises(ValueError, match="cannot be resolved locally"):
        build_balance_snapshot(db_session, rows, strict=True)


# ---------------------------------------------------------------------------
# contour_warehouse_refs — selected / ignored / finished-goods
# ---------------------------------------------------------------------------


def test_contour_warehouse_refs_respects_contour(db_session):
    db_session.add_all([
        models.StockWarehouse(warehouse_ref1c="wh-sel", warehouse_name="Sel", is_selected=True),
        models.StockWarehouse(warehouse_ref1c="wh-unsel", warehouse_name="Unsel", is_selected=False),
        models.StockWarehouse(warehouse_ref1c="wh-ign", warehouse_name="Ign", is_selected=True),
        models.StockWarehouse(
            warehouse_ref1c="wh-fg", warehouse_name="FG", is_selected=True, is_finished_goods=True
        ),
    ])
    db_session.add(models.IgnoredWarehouse(warehouse_ref1c="wh-ign"))
    db_session.flush()

    assert contour_warehouse_refs(db_session) == {"wh-sel"}


def test_contour_warehouse_refs_is_empty_without_settings(db_session):
    assert contour_warehouse_refs(db_session) == set()
