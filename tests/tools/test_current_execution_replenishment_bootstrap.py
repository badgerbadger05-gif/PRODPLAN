"""Contracts of the one-off ``--phase replenishment-bootstrap`` migration step.

A freshly migrated database has no R4 current replenishment state at all: the
bounded physical path only replays the BUY scopes a delta touched.  This phase
calls the one canonical accepted-generation writer; the tests here pin the
guards around that call, so the real writer is patched on purpose.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app import models
from tools.current_execution_migration import (
    PostflightBlocked,
    PreflightBlocked,
    apply_current_replenishment_bootstrap,
)

CUTOFF = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
POSTING_AT = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
#: One of the canonical supplier document types the provenance writer types.
SUPPLIER_RECORDER_TYPE = "Document_\u041f\u0440\u0438\u0445\u043e\u0434\u043d\u0430\u044f\u041d\u0430\u043a\u043b\u0430\u0434\u043d\u0430\u044f"

#: The physical prefix is real here: the bootstrap now has to prove that an
#: empty provenance table is not the same thing as a generation with no
#: supplier facts, and that proof runs the canonical visibility query.
_PHYSICAL_TABLES = (
    "physical_import_batch",
    "physical_import_page",
    "ledger_generation",
    "stock_ledger_entry",
    "stock_ledger_fact_supersession",
)


def _engine():
    return create_engine("sqlite:///:memory:")


def _schema(engine):
    models.Base.metadata.create_all(
        engine,
        tables=[models.Base.metadata.tables[name] for name in _PHYSICAL_TABLES],
    )
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE planning_truth_state ("
            "id INTEGER PRIMARY KEY, current_generation_id INTEGER)"
        ))
        connection.execute(text(
            "CREATE TABLE stock_ledger_supplier_receipt_provenance ("
            "id INTEGER PRIMARY KEY, ledger_generation_id INTEGER NOT NULL, "
            "stock_ledger_entry_id INTEGER NOT NULL, match_status TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE current_replenishment_state ("
            "id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL, status TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE reservation_consumption_allocation ("
            "id INTEGER PRIMARY KEY, allocation_role TEXT NOT NULL, is_current BOOLEAN NOT NULL)"
        ))


def _seed(
    engine,
    *,
    pointer=7,
    provenance_generation=7,
    exact_rows=2,
    supplier_sle_rows=None,
):
    """Seed the pointer generation over a real, complete import boundary.

    ``supplier_sle_rows`` defaults to one supplier ledger row per provenance
    row; passing it explicitly is how a stand carrying supplier facts with no
    provenance at all is reproduced.
    """
    sle_rows = int(exact_rows) if supplier_sle_rows is None else int(supplier_sle_rows)
    with engine.begin() as connection:
        connection.execute(models.PhysicalImportBatch.__table__.insert(), {
            "id": 1,
            "batch_key": "bootstrap-boundary",
            "status": "completed",
            "cutoff": CUTOFF,
            "source_watermarks": {"origin": "test"},
            "source_complete": True,
            "completed_at": CUTOFF,
        })
        connection.execute(models.LedgerGeneration.__table__.insert(), [
            {
                "id": generation_id,
                "generation_key": f"bootstrap-g{generation_id}",
                "status": "accepted",
                "cutoff": CUTOFF,
                "source_watermarks": {},
                "capabilities": {},
                "physical_import_batch_id": 1,
                "algorithm_version": "bootstrap-tests",
                "accepted_at": CUTOFF,
            }
            for generation_id in (7, 6)
        ])
        connection.execute(
            text("INSERT INTO planning_truth_state (id, current_generation_id) VALUES (1, :pointer)"),
            {"pointer": int(pointer)},
        )
        for index in range(1, sle_rows + 1):
            connection.execute(models.StockLedgerEntry.__table__.insert(), {
                "id": index,
                "ingest_batch_id": 1,
                "source_content_hash": f"{index:064d}",
                "item_id": index,
                "characteristic_ref": "",
                "organization_ref": "ORG",
                "warehouse_ref1c": "WH",
                "qty": Decimal("5"),
                "posting_at": POSTING_AT,
                "record_type": "Receipt",
                "recorder_type": SUPPLIER_RECORDER_TYPE,
                "recorder_ref": f"doc-{index}",
                "line_no": "1",
                "ingest_source": "document_pull",
                "active": True,
            })
        for index in range(1, int(exact_rows) + 1):
            connection.execute(text(
                "INSERT INTO stock_ledger_supplier_receipt_provenance "
                "(id, ledger_generation_id, stock_ledger_entry_id, match_status) "
                "VALUES (:id, :generation, :id, 'exact')"
            ), {"id": index, "generation": int(provenance_generation)})


def _writer(*, allocations=3, changed_pairs=4, states=2):
    calls: list[int] = []

    def _apply(session, *, generation_id):
        calls.append(int(generation_id))
        for index in range(1, int(states) + 1):
            session.execute(text(
                "INSERT INTO current_replenishment_state (id, scope_key, status) "
                "VALUES (:id, :scope, 'completed')"
            ), {"id": index, "scope": f"scope-{index}"})
        for index in range(1, int(allocations) + 1):
            session.execute(text(
                "INSERT INTO reservation_consumption_allocation "
                "(id, allocation_role, is_current) VALUES (:id, :role, 1)"
            ), {"id": index, "role": "supplier_receipt" if index % 2 else "consumption"})
        return tuple(
            SimpleNamespace(
                inserted=1, updated=0, deleted=0,
                changed_pairs=int(changed_pairs), audit_events=1, idempotent=False,
            )
            for _ in range(1)
        )

    _apply.calls = calls
    return _apply


def test_bootstrap_requires_explicit_writers_stopped(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine)
    writer = _writer()
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        writer,
    )

    with pytest.raises(PreflightBlocked, match="writers-stopped"):
        apply_current_replenishment_bootstrap(engine, writers_stopped=False)
    assert writer.calls == []


def test_bootstrap_blocks_when_another_generation_owns_the_provenance(monkeypatch):
    """Provenance stays at the generation that computed it; the pointer moved on."""

    engine = _engine()
    _schema(engine)
    _seed(engine, pointer=7, provenance_generation=6)
    writer = _writer()
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        writer,
    )

    with pytest.raises(PreflightBlocked, match=r"owned by g6\(2 rows\)"):
        apply_current_replenishment_bootstrap(engine, writers_stopped=True)
    assert writer.calls == []
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM current_replenishment_state"
        )).scalar_one() == 0


def test_bootstrap_reports_state_pairs_and_allocations_by_role(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine)
    writer = _writer(allocations=3, changed_pairs=4, states=2)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        writer,
    )

    report = apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    assert writer.calls == [7]
    assert report["status"] == "ready"
    assert report["generation_id"] == 7
    assert report["current_replenishment_state"] == 2
    assert report["changed_pairs"] == 4
    assert report["idempotent"] is False
    assert report["exact_provenance_rows"] == 2
    assert report["exact_provenance_rows_with_qty"] == 2
    assert report["reservation_consumption_allocation_current"] == {
        "total": 3,
        "by_allocation_role": {"consumption": 1, "supplier_receipt": 2},
    }


def test_second_bootstrap_run_reports_no_changes(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _writer(allocations=3, changed_pairs=4, states=2),
    )
    first = apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    def _idempotent(session, *, generation_id):
        return (
            SimpleNamespace(
                inserted=0, updated=0, deleted=0,
                changed_pairs=0, audit_events=0, idempotent=True,
            ),
        )

    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _idempotent,
    )
    second = apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["changed_pairs"] == 0
    assert second["current_replenishment_state"] == first["current_replenishment_state"]
    assert (
        second["reservation_consumption_allocation_current"]
        == first["reservation_consumption_allocation_current"]
    )


def test_zero_allocations_with_exact_provenance_fails_closed(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _writer(allocations=0, changed_pairs=9, states=554),
    )

    with pytest.raises(PostflightBlocked, match="0 current allocations"):
        apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM current_replenishment_state"
        )).scalar_one() == 0


def test_no_supplier_provenance_anywhere_is_a_legitimate_empty_bootstrap(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine, exact_rows=0)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _writer(allocations=0, changed_pairs=0, states=0),
    )

    report = apply_current_replenishment_bootstrap(engine, writers_stopped=True)
    assert report["status"] == "ready"
    assert report["exact_provenance_rows"] == 0
    assert report["reservation_consumption_allocation_current"]["total"] == 0


def test_empty_provenance_table_over_visible_supplier_facts_is_blocked(monkeypatch):
    """The hole the two old guards both missed.

    ``owned_here == 0 and owners`` only fires when some *other* generation
    owns the provenance, and the postflight only fires when exact rows with
    quantity exist.  With the provenance table entirely empty both are false,
    so the phase called the writer over live supplier facts, published zero
    allocations and reported success.
    """
    engine = _engine()
    _schema(engine)
    _seed(engine, exact_rows=0, supplier_sle_rows=3)
    writer = _writer(allocations=0, changed_pairs=0, states=0)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        writer,
    )

    with pytest.raises(PreflightBlocked, match="no supplier receipt provenance rows"):
        apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    assert writer.calls == []


def test_bootstrap_reports_provenance_and_visible_supplier_counts(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine, exact_rows=2)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _writer(allocations=3, changed_pairs=4, states=2),
    )

    report = apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    assert report["provenance_rows_at_pointer"] == 2
    assert report["supplier_receipt_sle_visible"] == 2


def test_no_supplier_facts_and_no_provenance_still_reports_both_zero(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine, exact_rows=0)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _writer(allocations=0, changed_pairs=0, states=0),
    )

    report = apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    assert report["status"] == "ready"
    assert report["provenance_rows_at_pointer"] == 0
    assert report["supplier_receipt_sle_visible"] == 0
