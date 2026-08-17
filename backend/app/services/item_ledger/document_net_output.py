"""Net output of one 1C assembly document.

``СборкаЗапасов`` writes a single physical movement as several lines of the
same register: an ``assembly_in`` on the production warehouse, an
``assembly_out`` leaving it and an ``assembly_in`` on the destination
warehouse.  The physical Ledger is right — the balance of ``+N -N +N`` is
``+N`` — but only ``N`` was produced.  Reading every ``assembly_in`` as output
counts the internal transport of one document as production and inflates both
replenishment execution and plan output.

This module owns that one quantity for every reader:

```text
document_output_qty = max(sum(assembly_in) - sum(assembly_out), 0)
```

per ``(recorder_ref, item_id, characteristic_ref, organization_ref)``, spread
deterministically over the ``assembly_in`` lines of the same document.  It has
no persistence and no allocation policy: callers keep owning what they do with
a fact, this module only says how much of it exists.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable


OUTPUT_MOVEMENT_KIND = "assembly_in"
OFFSET_MOVEMENT_KIND = "assembly_out"
NETTED_MOVEMENT_KINDS = frozenset({OUTPUT_MOVEMENT_KIND, OFFSET_MOVEMENT_KIND})


def _qty(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _kind(row: Any) -> str:
    return str(row.movement_kind or "")


def _document_key(row: Any) -> tuple[str, int, str, str]:
    """Identity of one physical movement inside one 1C document."""
    recorder = str(row.recorder_ref or "").strip()
    return (
        recorder or f"__sle__:{int(row.id)}",
        int(row.item_id),
        str(row.characteristic_ref or ""),
        str(row.organization_ref or ""),
    )


def _document_line_order(row: Any) -> tuple[Any, ...]:
    """Deterministic in-document order: the document line, then the row id.

    Lines of one document share a posting time, so ordering deliberately does
    not read it: ``line_no`` is the document's own order and ``id`` breaks any
    remaining tie.
    """
    raw = str(row.line_no or "").strip()
    line_key = (0, int(raw), "") if raw.isdigit() else (1, 0, raw)
    return (line_key, int(row.id))


def net_document_output_qty(rows: Iterable[Any]) -> dict[int, Decimal]:
    """Return the net output quantity of every ``assembly_in`` row.

    Offsetting is warehouse-local first, so the surviving quantity stays on the
    warehouse where the stock actually remained instead of on an arbitrary leg.
    An ``assembly_out`` that no ``assembly_in`` of its own warehouse covers —
    stock taken from an earlier period — then reduces the remaining lines in
    document order.  Whatever the warehouse layout, the document total is
    always ``max(sum(assembly_in) - sum(assembly_out), 0)``.

    Rows of different documents never net against each other.
    """
    grouped: dict[tuple[str, int, str, str], list[Any]] = {}
    for row in rows:
        if _kind(row) not in NETTED_MOVEMENT_KINDS:
            continue
        grouped.setdefault(_document_key(row), []).append(row)

    net_by_sle: dict[int, Decimal] = {}
    for document_rows in grouped.values():
        ordered = sorted(document_rows, key=_document_line_order)
        by_warehouse: dict[str, list[Any]] = {}
        for row in ordered:
            by_warehouse.setdefault(str(row.warehouse_ref1c or ""), []).append(row)

        unmatched_offset = Decimal("0")
        for warehouse_rows in by_warehouse.values():
            offset_left = sum(
                (
                    abs(_qty(row.qty))
                    for row in warehouse_rows
                    if _kind(row) == OFFSET_MOVEMENT_KIND
                ),
                Decimal("0"),
            )
            for row in warehouse_rows:
                if _kind(row) != OUTPUT_MOVEMENT_KIND:
                    continue
                available = abs(_qty(row.qty))
                take = min(available, offset_left)
                offset_left -= take
                net_by_sle[int(row.id)] = available - take
            unmatched_offset += offset_left

        for row in ordered:
            if unmatched_offset <= 0:
                break
            if _kind(row) != OUTPUT_MOVEMENT_KIND:
                continue
            available = net_by_sle.get(int(row.id), Decimal("0"))
            take = min(available, unmatched_offset)
            net_by_sle[int(row.id)] = available - take
            unmatched_offset -= take

    return net_by_sle
