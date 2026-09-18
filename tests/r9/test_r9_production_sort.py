from types import SimpleNamespace

from app import models
from app.services import production_control_journal_snapshot as journal


def _read_sorted(db, monkeypatch, rows, *, field=None):
    snapshot = SimpleNamespace(
        id=1,
        ledger_generation_id=1,
        payload={"meta": {"read_only": True, "ledger_generation_id": 1}},
    )
    monkeypatch.setattr(journal, "get_latest_read_snapshot", lambda *a, **kw: snapshot)
    monkeypatch.setattr(journal, "overlay_launch_facts", lambda *a, **kw: None)
    monkeypatch.setattr(journal, "overlay_execution_state", lambda *a, **kw: None)
    for index, row in enumerate(rows, start=1):
        db.add(models.PlanningReadRow(
            snapshot_id=1,
            row_kind=journal.ROW_KIND,
            row_key=f"product:{index}",
            payload={**row, "planned_start_date": row["order_date"]},
        ))
    db.flush()
    return journal.read_snapshot(db, sort_by=field, sort_dir="desc")["rows"]


def _row(order_number, order_date, line_number):
    return {
        "order_number": order_number,
        "order_date": order_date,
        "line_number": line_number,
    }


def test_default_journal_sort_puts_non_null_dates_desc_and_ties_asc(db_session, monkeypatch):
    rows = [
        _row("B", None, 1),
        _row("A", "2026-09-10", 2),
        _row("A", "2026-09-10", 1),
        _row("C", "2026-09-09", 1),
    ]

    rows = _read_sorted(db_session, monkeypatch, rows)

    assert [(r["order_date"], r["order_number"], r["line_number"]) for r in rows] == [
        ("2026-09-10", "A", 1),
        ("2026-09-10", "A", 2),
        ("2026-09-09", "C", 1),
        (None, "B", 1),
    ]


def test_explicit_descending_sort_keeps_ties_ascending_and_nulls_last(db_session, monkeypatch):
    rows = [
        _row("B", None, 1),
        _row("A", "2026-09-09", 2),
        _row("A", "2026-09-09", 1),
        _row("C", "2026-09-10", 1),
    ]

    rows = _read_sorted(db_session, monkeypatch, rows, field="planned_start_date")

    assert [(r["order_date"], r["order_number"], r["line_number"]) for r in rows] == [
        ("2026-09-10", "C", 1),
        ("2026-09-09", "A", 1),
        ("2026-09-09", "A", 2),
        (None, "B", 1),
    ]
