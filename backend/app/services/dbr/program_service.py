"""DBR production program — CRUD + approve.

Persistence over dbr_production_program(+_item). Functions take a live Session
and do not commit (caller owns the transaction), matching project convention.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from ...models import DbrProductionProgram, DbrProductionProgramItem

DRAFT = "draft"
APPROVED = "approved"
CLOSED = "closed"
CANCELLED = "cancelled"

_EDITABLE = (DRAFT,)


def _validate_items(
    items: Iterable[dict[str, Any]], *, from_date: date, to_date: date
) -> list[dict[str, Any]]:
    """Normalize and validate the complete set of program rows."""
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, date]] = set()
    for row in items:
        item_id = int(row["item_id"])
        program_date = row["program_date"]
        try:
            qty = Decimal(str(row["qty"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("количество в строке программы должно быть числом больше нуля") from exc
        if not qty.is_finite() or qty <= 0:
            raise ValueError("количество в строке программы должно быть больше нуля")
        if not from_date <= program_date <= to_date:
            raise ValueError(
                f"дата строки {program_date} находится вне периода программы "
                f"{from_date} — {to_date}"
            )
        key = (item_id, program_date)
        if key in seen:
            raise ValueError(
                f"дубликат строки: номенклатура {item_id} уже задана на дату {program_date}"
            )
        seen.add(key)
        normalized.append({**row, "item_id": item_id, "qty": qty})
    return normalized


def _current_item_rows(program: DbrProductionProgram) -> list[dict[str, Any]]:
    return [
        {
            "item_id": row.item_id,
            "program_date": row.program_date,
            "qty": row.qty,
            "comment": row.comment,
        }
        for row in program.items
    ]


def _set_items(db: Session, program: DbrProductionProgram, items: Iterable[dict[str, Any]]) -> None:
    for row in items:
        # Append through the relationship so program.items stays in sync within
        # the session (a plain db.add would leave the loaded collection stale).
        program.items.append(
            DbrProductionProgramItem(
                item_id=int(row["item_id"]),
                program_date=row["program_date"],
                qty=row["qty"],
                comment=row.get("comment"),
            )
        )


def create_program(
    db: Session,
    *,
    from_date: date,
    to_date: date,
    company: Optional[str] = None,
    title: Optional[str] = None,
    created_by: Optional[str] = None,
    items: Optional[Iterable[dict[str, Any]]] = None,
) -> DbrProductionProgram:
    if from_date > to_date:
        raise ValueError("from_date позже to_date")
    normalized_items = _validate_items(
        items or [], from_date=from_date, to_date=to_date
    )
    program = DbrProductionProgram(
        company=company,
        title=title,
        from_date=from_date,
        to_date=to_date,
        created_by=created_by,
        status=DRAFT,
    )
    db.add(program)
    db.flush()
    if normalized_items:
        _set_items(db, program, normalized_items)
    db.flush()
    return program


def get_program(db: Session, program_id: int) -> Optional[DbrProductionProgram]:
    return db.get(DbrProductionProgram, program_id)


def list_programs(db: Session, status: Optional[str] = None) -> list[DbrProductionProgram]:
    query = db.query(DbrProductionProgram)
    if status:
        query = query.filter(DbrProductionProgram.status == status)
    return query.order_by(DbrProductionProgram.id.desc()).all()


def update_program(db: Session, program_id: int, data: dict[str, Any]) -> DbrProductionProgram:
    program = db.get(DbrProductionProgram, program_id)
    if program is None:
        raise LookupError("program not found")
    if program.status not in _EDITABLE:
        raise ValueError(f"программу в статусе «{program.status}» редактировать нельзя")

    next_from = data.get("from_date") or program.from_date
    next_to = data.get("to_date") or program.to_date
    if next_from > next_to:
        raise ValueError("from_date позже to_date")
    candidate_items = data.get("items")
    if candidate_items is None:
        candidate_items = _current_item_rows(program)
    normalized_items = _validate_items(
        candidate_items, from_date=next_from, to_date=next_to
    )

    for field in ("company", "title", "from_date", "to_date"):
        if field in data and data[field] is not None:
            setattr(program, field, data[field])

    if "items" in data and data["items"] is not None:
        # delete-orphan cascade removes the cleared rows on flush.
        program.items.clear()
        db.flush()
        _set_items(db, program, normalized_items)
    db.flush()
    return program


def approve_program(db: Session, program_id: int) -> DbrProductionProgram:
    program = db.get(DbrProductionProgram, program_id)
    if program is None:
        raise LookupError("program not found")
    if program.status not in (DRAFT, APPROVED):
        raise ValueError(f"утвердить можно только черновик (статус сейчас «{program.status}»)")
    if not program.items:
        raise ValueError("в программе нет строк — утверждать нечего")
    _validate_items(
        _current_item_rows(program),
        from_date=program.from_date,
        to_date=program.to_date,
    )
    if program.status == APPROVED:
        return program
    program.status = APPROVED
    db.flush()
    return program
