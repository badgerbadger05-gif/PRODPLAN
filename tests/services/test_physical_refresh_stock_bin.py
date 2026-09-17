from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event

from app import models
from app.services.item_ledger.physical import LedgerKey
from app.services.item_ledger.physical_refresh_stock_bin import (
    BoundedPhysicalDeltaManifest,
    BoundedStockBinRefreshError,
    apply_bounded_current_stock_bins,
)


def _setup_refresh(db, *, key="stock-bin-refresh"):
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    parent_batch = models.PhysicalImportBatch(
        batch_key=f"{key}:parent", status="completed", cutoff=cutoff,
        completed_at=cutoff, source_watermarks={"source": "test"},
    )
    target_batch = models.PhysicalImportBatch(
        batch_key=f"{key}:target", status="completed", cutoff=cutoff + timedelta(days=1),
        completed_at=cutoff + timedelta(days=1), source_watermarks={"source": "test"},
    )
    parent = models.LedgerGeneration(
        generation_key=f"{key}:parent", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=parent_batch, algorithm_version="test",
    )
    target = models.LedgerGeneration(
        generation_key=f"{key}:target", status="building",
        cutoff=cutoff + timedelta(days=1), source_watermarks={}, capabilities={},
        physical_import_batch=target_batch, algorithm_version="test",
    )
    item = models.Item(item_code=f"{key}:item", item_name="Bounded stock", unit="шт")
    db.add_all([parent_batch, target_batch, parent, target, item])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db.flush()
    return parent, target, item


def _entry(db, batch, item, qty, *, warehouse="WH-A", ref="doc", posting=None):
    row = models.StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash=(f"{ref}:{qty}").encode().hex().ljust(64, "0")[:64],
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="ORG-A",
        warehouse_ref1c=warehouse,
        qty=Decimal(str(qty)),
        qty_after=Decimal(str(qty)),
        posting_at=posting or batch.cutoff,
        known_at=posting or batch.cutoff,
        record_type="Receipt" if Decimal(str(qty)) >= 0 else "Expense",
        movement_kind="receipt" if Decimal(str(qty)) >= 0 else "expense",
        recorder_type="Document_Test",
        recorder_ref=ref,
        line_no="1",
        ingest_source="test",
    )
    db.add(row)
    db.flush()
    return row


def _current_bin(db, generation, item, *, warehouse="WH-A", quantity=0):
    row = models.StockBin(
        ledger_generation_id=generation.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="ORG-A", warehouse_ref1c=warehouse,
        on_hand=Decimal(str(quantity)), last_entry_id=None, is_current=True,
    )
    db.add(row)
    db.flush()
    return row


def test_empty_affected_scope_is_true_noop(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-empty")
    row = _current_bin(db_session, parent, item, quantity=4)
    before = (row.id, row.ledger_generation_id, row.on_hand, row.last_entry_id)

    result = apply_bounded_current_stock_bins(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        affected_physical_keys=[],
        delta_manifest=BoundedPhysicalDeltaManifest(),
    )

    assert result.changed_rows == 0
    assert result.visible_fact_rows == 0
    assert (row.id, row.ledger_generation_id, row.on_hand, row.last_entry_id) == before


def test_empty_manifest_for_declared_key_does_not_rewrite_current_owner(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-key-noop")
    row = _current_bin(db_session, parent, item, quantity=4)
    before = (row.id, row.ledger_generation_id, row.on_hand, row.last_entry_id)

    result = apply_bounded_current_stock_bins(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        affected_physical_keys=[(item.item_id, "", "ORG-A", "WH-A")],
        delta_manifest=BoundedPhysicalDeltaManifest(),
    )

    assert result.changed_keys == ()
    assert result.semantic_noop_keys == (LedgerKey(item.item_id, "", "ORG-A", "WH-A"),)
    db_session.refresh(row)
    assert (row.id, row.ledger_generation_id, row.on_hand, row.last_entry_id) == before


def test_one_affected_key_updates_existing_owner_and_leaves_neighbor_stable(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-one")
    other = models.Item(item_code="stock-bin-one:other", item_name="Other", unit="шт")
    db_session.add(other)
    db_session.flush()
    parent_row = _current_bin(db_session, parent, item, quantity=10)
    neighbor = _current_bin(db_session, parent, other, quantity=7, warehouse="WH-B")
    _entry(db_session, parent.physical_import_batch, item, 10, ref="opening")
    issue = _entry(db_session, target.physical_import_batch, item, -3, ref="issue")
    db_session.flush()
    neighbor_before = (neighbor.id, neighbor.ledger_generation_id, neighbor.on_hand, neighbor.last_entry_id)

    result = apply_bounded_current_stock_bins(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        affected_physical_keys=[LedgerKey(item.item_id, "", "ORG-A", "WH-A")],
        delta_manifest={"new_sle_ids": [issue.id]},
    )

    db_session.refresh(parent_row)
    assert result.changed_rows == 1
    assert parent_row.ledger_generation_id == target.id
    assert parent_row.on_hand == Decimal("7")
    assert (neighbor.id, neighbor.ledger_generation_id, neighbor.on_hand, neighbor.last_entry_id) == neighbor_before


def test_multi_key_manifest_partitions_delta_and_allows_declared_no_delta_key(db_session):
    parent, target, item_one = _setup_refresh(db_session, key="stock-bin-multi")
    item_two = models.Item(item_code="stock-bin-multi:two", item_name="Two", unit="шт")
    item_three = models.Item(item_code="stock-bin-multi:three", item_name="Three", unit="шт")
    undeclared = models.Item(item_code="stock-bin-multi:other", item_name="Other", unit="шт")
    db_session.add_all([item_two, item_three, undeclared])
    db_session.flush()
    row_one = _current_bin(db_session, parent, item_one, quantity=10)
    row_two = _current_bin(db_session, parent, item_two, quantity=4)
    row_three = _current_bin(db_session, parent, item_three, quantity=7)
    neighbor = _current_bin(db_session, parent, undeclared, quantity=9)
    delta_one = _entry(db_session, target.physical_import_batch, item_one, -2, ref="one")
    delta_two = _entry(db_session, target.physical_import_batch, item_two, 3, ref="two")
    key_one = LedgerKey(item_one.item_id, "", "ORG-A", "WH-A")
    key_two = LedgerKey(item_two.item_id, "", "ORG-A", "WH-A")
    key_three = LedgerKey(item_three.item_id, "", "ORG-A", "WH-A")
    neighbor_before = (neighbor.id, neighbor.ledger_generation_id, neighbor.on_hand)

    result = apply_bounded_current_stock_bins(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        affected_physical_keys=[key_three, key_one, key_two],
        delta_manifest={"new_sle_ids": [delta_two.id, delta_one.id]},
    )

    db_session.refresh(row_one)
    db_session.refresh(row_two)
    db_session.refresh(row_three)
    assert result.delta_rows == 2
    assert set(result.changed_keys) == {key_one, key_two}
    assert result.semantic_noop_keys == (key_three,)
    assert (row_one.ledger_generation_id, row_one.on_hand) == (target.id, Decimal("8"))
    assert (row_two.ledger_generation_id, row_two.on_hand) == (target.id, Decimal("7"))
    assert (row_three.ledger_generation_id, row_three.on_hand) == (parent.id, Decimal("7"))
    assert (neighbor.id, neighbor.ledger_generation_id, neighbor.on_hand) == neighbor_before


def test_multi_key_refresh_bulk_loads_current_bins_and_last_entries(db_session):
    parent, target, first_item = _setup_refresh(db_session, key="stock-bin-bulk")
    second_item = models.Item(item_code="stock-bin-bulk:two", item_name="Two", unit="шт")
    third_item = models.Item(item_code="stock-bin-bulk:three", item_name="Three", unit="шт")
    db_session.add_all([second_item, third_item])
    db_session.flush()
    items = (first_item, second_item, third_item)
    keys = tuple(LedgerKey(item.item_id, "", "ORG-A", "WH-A") for item in items)
    for index, item in enumerate(items, 1):
        opening = _entry(db_session, parent.physical_import_batch, item, index, ref=f"opening-{index}")
        owner = _current_bin(db_session, parent, item, quantity=index)
        owner.last_entry_id = opening.id
    deltas = [
        _entry(db_session, target.physical_import_batch, item, 1, ref=f"delta-{index}")
        for index, item in enumerate(items, 1)
    ]
    db_session.flush()

    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        lowered = statement.lower()
        if "stock_bin" in lowered or "stock_ledger_entry" in lowered:
            statements.append(lowered)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = apply_bounded_current_stock_bins(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            affected_physical_keys=keys,
            delta_manifest={"new_sle_ids": [row.id for row in deltas]},
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    stock_bin_reads = [
        statement
        for statement in statements
        if statement.lstrip().startswith("select") and "stock_bin" in statement
    ]
    assert len(stock_bin_reads) == 1
    assert not any(
        "stock_ledger_entry.id = ?" in statement
        for statement in statements
    )
    assert result.changed_rows == len(keys)


def test_manifest_rejects_row_or_edge_from_undeclared_full_key(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-foreign")
    foreign = models.Item(item_code="stock-bin-foreign:other", item_name="Other", unit="шт")
    db_session.add(foreign)
    db_session.flush()
    row = _current_bin(db_session, parent, item, quantity=5)
    foreign_old = _entry(db_session, parent.physical_import_batch, foreign, 2, ref="foreign-old")
    foreign_new = _entry(db_session, target.physical_import_batch, foreign, 3, ref="foreign-new")
    edge = models.StockLedgerFactSupersession(
        old_sle_id=foreign_old.id, new_sle_id=foreign_new.id,
        import_batch_id=target.physical_import_batch.id,
    )
    db_session.add(edge)
    db_session.flush()

    with pytest.raises(BoundedStockBinRefreshError, match="outside affected physical"):
        apply_bounded_current_stock_bins(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            affected_physical_keys=[(item.item_id, "", "ORG-A", "WH-A")],
            delta_manifest={"new_sle_ids": [foreign_new.id], "supersession_edge_ids": [edge.id]},
        )
    db_session.refresh(row)
    assert (row.ledger_generation_id, row.on_hand) == (parent.id, Decimal("5"))


def test_manifest_rejects_supersession_edge_from_undeclared_full_key(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-foreign-edge")
    row = _current_bin(db_session, parent, item, quantity=5)
    foreign_old = _entry(
        db_session, parent.physical_import_batch, item, 2,
        warehouse="WH-B", ref="foreign-edge-old",
    )
    edge = models.StockLedgerFactSupersession(
        old_sle_id=foreign_old.id, new_sle_id=None,
        import_batch_id=target.physical_import_batch.id,
    )
    db_session.add(edge)
    db_session.flush()

    with pytest.raises(BoundedStockBinRefreshError, match="undeclared affected key"):
        apply_bounded_current_stock_bins(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            affected_physical_keys=[(item.item_id, "", "ORG-A", "WH-A")],
            delta_manifest={"supersession_edge_ids": [edge.id]},
        )
    db_session.refresh(row)
    assert (row.ledger_generation_id, row.on_hand) == (parent.id, Decimal("5"))


def test_supersession_uses_target_visible_replacement_once(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-correction")
    row = _current_bin(db_session, parent, item, quantity=5)
    old = _entry(db_session, parent.physical_import_batch, item, 5, ref="old")
    replacement = _entry(db_session, target.physical_import_batch, item, 8, ref="replacement")
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=old.id, new_sle_id=replacement.id, import_batch_id=target.physical_import_batch.id,
    ))
    db_session.flush()
    edge = db_session.query(models.StockLedgerFactSupersession).one()

    result = apply_bounded_current_stock_bins(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        affected_physical_keys=[(item.item_id, "", "ORG-A", "WH-A")],
        delta_manifest={"new_sle_ids": [replacement.id], "supersession_edge_ids": [edge.id]},
    )

    db_session.refresh(row)
    assert result.visible_fact_rows == 1
    assert row.on_hand == Decimal("8")
    assert row.last_entry_id == replacement.id


def test_retry_same_target_is_idempotent_without_second_row_change(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-retry")
    row = _current_bin(db_session, parent, item, quantity=2)
    _entry(db_session, parent.physical_import_batch, item, 2, ref="opening")
    receipt = _entry(db_session, target.physical_import_batch, item, 3, ref="receipt")
    key = (item.item_id, "", "ORG-A", "WH-A")

    first = apply_bounded_current_stock_bins(
        db_session, target_generation_id=target.id, parent_generation_id=parent.id,
        affected_physical_keys=[key],
        delta_manifest={"new_sle_ids": [receipt.id]},
    )
    db_session.flush()
    before = (row.id, row.ledger_generation_id, row.on_hand, row.last_entry_id)
    second = apply_bounded_current_stock_bins(
        db_session, target_generation_id=target.id, parent_generation_id=parent.id,
        affected_physical_keys=[key],
        delta_manifest={"new_sle_ids": [receipt.id]},
    )

    assert first.changed_rows == 1
    assert second.changed_rows == 0
    assert second.semantic_noop_keys == (LedgerKey(*key),)
    assert (row.id, row.ledger_generation_id, row.on_hand, row.last_entry_id) == before


def test_stale_parent_and_incomplete_key_fail_closed(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-stale")
    db_session.get(models.PlanningTruthState, 1).current_generation_id = target.id
    with pytest.raises(BoundedStockBinRefreshError, match="current truth"):
        apply_bounded_current_stock_bins(
            db_session, target_generation_id=target.id, parent_generation_id=parent.id,
            affected_physical_keys=[(item.item_id, "", "ORG-A", "WH-A")],
            delta_manifest=BoundedPhysicalDeltaManifest(),
        )


def test_bounded_path_never_calls_historical_visibility_prefix(db_session, monkeypatch):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-no-prefix")
    row = _current_bin(db_session, parent, item, quantity=1)
    receipt = _entry(db_session, target.physical_import_batch, item, 2, ref="delta")

    def explode(*args, **kwargs):
        raise AssertionError("historical visibility prefix must not be read")

    import app.services.item_ledger.physical_visibility as visibility
    monkeypatch.setattr(visibility, "visible_sle_query", explode)
    result = apply_bounded_current_stock_bins(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        affected_physical_keys=[(item.item_id, "", "ORG-A", "WH-A")],
        delta_manifest={"new_sle_ids": [receipt.id]},
    )

    db_session.refresh(row)
    assert result.changed_rows == 1
    assert row.on_hand == Decimal("3")


def test_malformed_delta_manifest_fails_closed_before_stock_change(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-malformed")
    row = _current_bin(db_session, parent, item, quantity=5)
    replacement = _entry(db_session, target.physical_import_batch, item, 8, ref="replacement")
    old = _entry(db_session, parent.physical_import_batch, item, 5, ref="old")
    edge = models.StockLedgerFactSupersession(
        old_sle_id=old.id, new_sle_id=replacement.id,
        import_batch_id=target.physical_import_batch.id,
    )
    db_session.add(edge)
    db_session.flush()

    with pytest.raises(BoundedStockBinRefreshError, match="incomplete"):
        apply_bounded_current_stock_bins(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            affected_physical_keys=[(item.item_id, "", "ORG-A", "WH-A")],
            delta_manifest={"supersession_edge_ids": [edge.id]},
        )
    db_session.refresh(row)
    assert row.ledger_generation_id == parent.id
    assert row.on_hand == Decimal("5")

    db_session.get(models.PlanningTruthState, 1).current_generation_id = parent.id
    with pytest.raises(BoundedStockBinRefreshError, match="incomplete"):
        apply_bounded_current_stock_bins(
            db_session, target_generation_id=target.id, parent_generation_id=parent.id,
            affected_physical_keys=[(item.item_id, None, "ORG-A", "WH-A")],
            delta_manifest=BoundedPhysicalDeltaManifest(),
        )


def test_backdated_delta_requires_explicit_bounded_boundary(db_session):
    parent, target, item = _setup_refresh(db_session, key="stock-bin-backdate")
    row = _current_bin(db_session, parent, item, quantity=1)
    correction_at = parent.cutoff - timedelta(hours=1)
    correction = _entry(
        db_session, target.physical_import_batch, item, 3, ref="backdate",
        posting=correction_at,
    )
    key = (item.item_id, "", "ORG-A", "WH-A")

    with pytest.raises(BoundedStockBinRefreshError, match="backdated"):
        apply_bounded_current_stock_bins(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            affected_physical_keys=[key],
            delta_manifest={"new_sle_ids": [correction.id]},
        )

    result = apply_bounded_current_stock_bins(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        affected_physical_keys=[key],
        delta_manifest={
            "new_sle_ids": [correction.id],
            "backdate_from": (parent.cutoff - timedelta(days=1)).isoformat(),
        },
    )
    db_session.refresh(row)
    assert result.changed_rows == 1
    assert row.on_hand == Decimal("4")
