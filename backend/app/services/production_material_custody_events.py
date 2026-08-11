from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session, joinedload

from ..models import (
    ProductionMaterialCustodyEvent,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    StockLedgerEntry,
    SyncLink,
)
from .production_control_common import to_float as _to_float
from .production_control_common import to_float_strict as _to_float_strict
from .one_c_export_common import clean_ref1c as _clean_ref1c

_EPSILON = 1.0e-9
_IDEMPOTENCY_PREFIX = "custody-event"
_STOCK_TRANSFER_ENTITY = "Document_ПеремещениеЗапасов"


def _custody_event_idempotency_key(
    *,
    issue_id: int,
    line_id: int,
    revision: int,
    source_kind: str,
    location_kind: str,
    warehouse_ref1c: str,
    delta_qty: float,
    source_sle_id: Optional[int],
    source_sle_identity: Optional[str] = None,
) -> str:
    key_parts = [
        str(int(issue_id)),
        str(int(line_id)),
        str(source_kind),
        str(location_kind),
        str(_clean_ref1c(warehouse_ref1c) or ""),
        f"{_to_float_strict(delta_qty, field='delta_qty'):.6f}",
    ]
    if source_sle_identity:
        key_parts.append(f"physical:{source_sle_identity}")
    elif source_sle_id is not None:
        key_parts.append(f"sle:{int(source_sle_id)}")
    else:
        key_parts.append(f"rev:{int(revision)}")

    payload = "|".join(key_parts)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"{_IDEMPOTENCY_PREFIX}:{digest}"


def stable_physical_sle_identity(sle: StockLedgerEntry) -> Optional[str]:
    """Stable identity of one imported physical Ledger line across reimports."""
    content_hash = str(sle.source_content_hash or "").strip()
    recorder_type = str(sle.recorder_type or "").strip()
    recorder_ref = str(sle.recorder_ref or "").strip()
    line_no = str(sle.line_no or "").strip()
    if not all((content_hash, recorder_type, recorder_ref, line_no)):
        return None
    return "|".join((content_hash, recorder_type, recorder_ref, line_no))


def _next_revision(line: ProductionMaterialIssueLine) -> int:
    return int(getattr(line, "custody_event_revision", 0) or 0) + 1


def _clean_ref2c(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip()
    return raw or None


def append_material_issue_custody_event(
    db: Session,
    *,
    issue: ProductionMaterialIssue,
    line: ProductionMaterialIssueLine,
    delta_qty: float,
    source_kind: str,
    location_kind: str,
    warehouse_ref1c: str,
    source_ref1c: Optional[str] = None,
    source_ref2c: Optional[str] = None,
    effective_at: Optional[datetime] = None,
    source_sle_id: Optional[int] = None,
    source_sle_identity: Optional[str] = None,
) -> bool:
    """
    Append one custody delta event for a material-issue line, guarded by an
    idempotency key derived from mutation coordinates.

    Returns True iff the event was appended.
    """
    delta = _to_float_strict(delta_qty, field="delta_qty")
    if abs(delta) <= _EPSILON:
        return False

    issue_id = int(getattr(issue, "issue_id", 0))
    if not issue_id:
        raise ValueError("issue_id is required for custody events")

    line_id = int(getattr(line, "line_id", 0) or 0)
    if not line_id:
        db.flush()
        line_id = int(getattr(line, "line_id", 0) or 0)

    if not line_id:
        raise RuntimeError("line_id could not be assigned before appending custody event")

    warehouse = _clean_ref1c(warehouse_ref1c)
    if not warehouse:
        raise ValueError("warehouse_ref1c is required for custody events")

    revision = _next_revision(line)
    key = _custody_event_idempotency_key(
        issue_id=issue_id,
        line_id=line_id,
        revision=revision,
        source_kind=source_kind,
        location_kind=location_kind,
        warehouse_ref1c=warehouse,
        delta_qty=delta,
        source_sle_id=source_sle_id,
        source_sle_identity=source_sle_identity,
    )
    existing = (
        db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.idempotency_key == key)
        .one_or_none()
    )
    if existing is not None:
        return False

    line.custody_event_revision = revision
    db.add(
        ProductionMaterialCustodyEvent(
            issue_id=issue_id,
            product_id=int(issue.product_id),
            component_item_id=int(line.component_item_id),
            source_kind=source_kind,
            source_sle_id=source_sle_id,
            effective_at=effective_at or datetime.now(timezone.utc),
            location_kind=location_kind,
            warehouse_ref1c=warehouse,
            source_ref1c=_clean_ref1c(source_ref1c),
            source_ref2c=_clean_ref2c(source_ref2c),
            delta_qty=delta,
            idempotency_key=key,
            document_number=str(issue.document_number or ""),
            document_line_no=str(line.line_id) if int(getattr(line, "line_id", 0) or 0) else None,
        )
    )
    return True


def append_material_issue_custody_terminal_release(
    db: Session,
    *,
    issue: ProductionMaterialIssue,
    line: ProductionMaterialIssueLine,
    delta_qty: float,
    location_kind: str,
    warehouse_ref1c: str,
    effective_at=None,
) -> bool:
    return append_material_issue_custody_event(
        db,
        issue=issue,
        line=line,
        delta_qty=delta_qty,
        source_kind="terminal_release",
        location_kind=location_kind,
        warehouse_ref1c=warehouse_ref1c,
        source_ref1c=str(issue.source_warehouse_ref1c or None),
        effective_at=effective_at,
    )


def _has_transit_issue_opening(
    db: Session,
    *,
    issue: ProductionMaterialIssue,
    line: ProductionMaterialIssueLine,
) -> bool:
    """Whether this issue line already has its append-only transit opening."""
    return (
        db.query(ProductionMaterialCustodyEvent.id)
        .filter(
            ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id),
            ProductionMaterialCustodyEvent.component_item_id == int(line.component_item_id),
            ProductionMaterialCustodyEvent.source_kind == "issue_created",
            ProductionMaterialCustodyEvent.location_kind == "transit",
            ProductionMaterialCustodyEvent.delta_qty > 0,
        )
        .first()
        is not None
    )


def _has_equivalent_physical_custody_event(
    db: Session,
    *,
    issue: ProductionMaterialIssue,
    line: ProductionMaterialIssueLine,
    source_kind: str,
    location_kind: str,
    warehouse_ref1c: str,
    sle: StockLedgerEntry,
) -> bool:
    """Find a prior event for the same stable SLE line, not merely its row id."""
    identity = stable_physical_sle_identity(sle)
    if identity is None:
        return False
    content_hash, recorder_type, recorder_ref, line_no = identity.split("|", 3)
    return (
        db.query(ProductionMaterialCustodyEvent.id)
        .join(
            StockLedgerEntry,
            ProductionMaterialCustodyEvent.source_sle_id == StockLedgerEntry.id,
        )
        .filter(
            ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id),
            ProductionMaterialCustodyEvent.component_item_id == int(line.component_item_id),
            ProductionMaterialCustodyEvent.source_kind == str(source_kind),
            ProductionMaterialCustodyEvent.location_kind == str(location_kind),
            ProductionMaterialCustodyEvent.warehouse_ref1c == _clean_ref1c(warehouse_ref1c),
            StockLedgerEntry.source_content_hash == content_hash,
            StockLedgerEntry.recorder_type == recorder_type,
            StockLedgerEntry.recorder_ref == recorder_ref,
            StockLedgerEntry.line_no == line_no,
        )
        .first()
        is not None
    )


def project_transfer_custody_events_for_recorder(
    db: Session,
    *,
    recorder_type: str,
    recorder_ref: str,
    stock_ledger_entries: Iterable[StockLedgerEntry],
) -> int:
    """Append custody changes only from freshly persisted physical SLE facts."""
    if str(recorder_type or "") != _STOCK_TRANSFER_ENTITY:
        return 0
    ref = str(recorder_ref or "").strip()
    if not ref:
        return 0
    link = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.target_entity == _STOCK_TRANSFER_ENTITY,
            SyncLink.target_ref_key == ref,
        )
        .order_by(SyncLink.link_id.desc())
        .first()
    )
    if link is None:
        return 0
    issue = (
        db.query(ProductionMaterialIssue)
        .options(joinedload(ProductionMaterialIssue.lines))
        .filter(ProductionMaterialIssue.issue_id == int(link.source_id))
        .one_or_none()
    )
    if issue is None:
        return 0
    direction = str(issue.direction or "")
    source = _clean_ref1c(issue.source_warehouse_ref1c)
    destination = _clean_ref1c(issue.warehouse_ref1c)
    if direction not in {"issue", "return"} or (direction == "issue" and source == destination):
        return 0

    lines: dict[int, ProductionMaterialIssueLine] = {}
    for line in issue.lines or []:
        component_id = int(line.component_item_id)
        if component_id in lines:
            raise RuntimeError(
                "material issue has duplicate component rows; custody allocation is ambiguous"
            )
        lines[component_id] = line
    appended = 0
    for sle in stock_ledger_entries:
        if (
            str(sle.recorder_type or "") != _STOCK_TRANSFER_ENTITY
            or str(sle.recorder_ref or "").strip() != ref
            or not bool(sle.active)
        ):
            raise ValueError("custody projector received an unrelated or inactive SLE row")
        line = lines.get(int(sle.item_id))
        movement = str(sle.movement_kind or "")
        qty = abs(_to_float_strict(sle.qty, field="ledger_event.qty"))
        if line is None or qty <= _EPSILON:
            continue
        if direction == "return":
            if movement != "transfer_out":
                continue
            source_kind, location, delta = "transfer_returned", "workshop", -qty
        elif movement == "transfer_out":
            # Older issue writers could export a transfer without recording its
            # transit reservation.  Establish the append-only opening before
            # the first physical outbound event, using the physical fact's
            # quantity and timestamp.  The lookup makes retries and later
            # recorder replays idempotent.
            if not _has_transit_issue_opening(db, issue=issue, line=line):
                if append_material_issue_custody_event(
                    db,
                    issue=issue,
                    line=line,
                    delta_qty=qty,
                    source_kind="issue_created",
                    location_kind="transit",
                    warehouse_ref1c=str(sle.warehouse_ref1c or ""),
                    source_ref1c=source,
                    source_ref2c=ref,
                    effective_at=sle.posting_at,
                ):
                    appended += 1
            source_kind, location, delta = "transfer_posted", "transit", -qty
        elif movement == "transfer_in":
            source_kind, location, delta = "transfer_posted", "workshop", qty
        else:
            continue
        if _has_equivalent_physical_custody_event(
            db,
            issue=issue,
            line=line,
            source_kind=source_kind,
            location_kind=location,
            warehouse_ref1c=str(sle.warehouse_ref1c or ""),
            sle=sle,
        ):
            continue
        if append_material_issue_custody_event(
            db,
            issue=issue,
            line=line,
            delta_qty=delta,
            source_kind=source_kind,
            location_kind=location,
            warehouse_ref1c=str(sle.warehouse_ref1c or ""),
            source_ref1c=source,
            source_ref2c=ref,
            source_sle_id=int(sle.id),
            source_sle_identity=stable_physical_sle_identity(sle),
            effective_at=sle.posting_at,
        ):
            appended += 1
    return appended
