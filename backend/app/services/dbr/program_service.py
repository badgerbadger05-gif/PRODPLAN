"""DBR production program — CRUD + approve.

Persistence over dbr_production_program(+_item). Functions take a live Session
and do not commit (caller owns the transaction), matching project convention.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from ...models import DbrProductionProgram, DbrProductionProgramItem

DRAFT = "draft"
APPROVED = "approved"
CLOSED = "closed"
CANCELLED = "cancelled"

_EDITABLE = (DRAFT,)


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
    if items:
        _set_items(db, program, items)
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

    for field in ("company", "title", "from_date", "to_date"):
        if field in data and data[field] is not None:
            setattr(program, field, data[field])
    if program.from_date > program.to_date:
        raise ValueError("from_date позже to_date")

    if "items" in data and data["items"] is not None:
        # delete-orphan cascade removes the cleared rows on flush.
        program.items.clear()
        db.flush()
        _set_items(db, program, data["items"])
    db.flush()
    return program


def approve_program(db: Session, program_id: int) -> DbrProductionProgram:
    program = db.get(DbrProductionProgram, program_id)
    if program is None:
        raise LookupError("program not found")
    if program.status == APPROVED:
        return program
    if program.status != DRAFT:
        raise ValueError(f"утвердить можно только черновик (статус сейчас «{program.status}»)")
    if not program.items:
        raise ValueError("в программе нет строк — утверждать нечего")
    program.status = APPROVED
    db.flush()
    return program
