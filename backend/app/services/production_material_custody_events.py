from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, func
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


def _custody_balance(
    db: Session,
    *,
    issue: ProductionMaterialIssue,
    line: ProductionMaterialIssueLine,
    location_kind: str,
    warehouse_ref1c: str,
    exclude_recorder_ref: str,
) -> float:
    """What one custody cell holds before a given transfer is folded into it.

    The document's own physical postings are excluded so a replay reads the same
    balance the first run did: without that, re-projecting a recorder would see
    its own consumption and refuse to finish a half-written pair.  Reservations
    of the same document are kept — the opening is exactly what the transfer is
    allowed to consume.
    """
    query = db.query(
        func.coalesce(func.sum(ProductionMaterialCustodyEvent.delta_qty), 0)
    ).filter(
        ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id),
        ProductionMaterialCustodyEvent.component_item_id
        == int(line.component_item_id),
        ProductionMaterialCustodyEvent.location_kind == str(location_kind),
        ProductionMaterialCustodyEvent.warehouse_ref1c
        == _clean_ref1c(warehouse_ref1c),
    )
    recorder = _clean_ref2c(exclude_recorder_ref)
    if recorder is not None:
        query = query.filter(
            ~and_(
                ProductionMaterialCustodyEvent.source_kind == "transfer_posted",
                ProductionMaterialCustodyEvent.source_ref2c == recorder,
            )
        )
    return _to_float(query.scalar() or 0)


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
    for line in sorted(issue.lines or [], key=lambda row: int(row.line_id or 0)):
        component_id = int(line.component_item_id)
        existing = lines.get(component_id)
        if existing is not None and (
            str(existing.unit or "").strip() != str(line.unit or "").strip()
            or int(existing.source_spec_id or 0) != int(line.source_spec_id or 0)
            or str(existing.line_status or "").strip()
            != str(line.line_status or "").strip()
        ):
            raise RuntimeError(
                "material issue has conflicting duplicate component rows; "
                "custody allocation is ambiguous"
            )
        # Older issue writers preserved repeated specification rows verbatim.
        # Custody is component-scoped and the physical transfer is already
        # aggregated by item, so equivalent rows share one deterministic event
        # coordinate.  Their reservation openings are still summed by
        # ``_custody_balance`` through issue_id + component_item_id.
        if existing is None:
            lines[component_id] = line
    appended = 0
    # Two passes.  A transfer may consume only what was reserved for it, and the
    # outbound and inbound rows of one component have to agree on that number
    # whatever order the Ledger hands them over in.
    movements: list[tuple[StockLedgerEntry, ProductionMaterialIssueLine, str, float]] = []
    outbound_qty: dict[int, float] = {}
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
        movements.append((sle, line, movement, qty))
        if direction == "issue" and movement == "transfer_out":
            component = int(line.component_item_id)
            outbound_qty[component] = outbound_qty.get(component, 0.0) + qty

    # How much of the shipment this issue actually holds.  A storekeeper may
    # ship more than was reserved (40.644 reserved, 49 moved).  The surplus does
    # arrive at the workshop, but it is nobody's reservation: it stays free
    # stock there for the next issue.  Folding it as custody of this product
    # instead drove the transit cell negative and failed the whole Ledger build
    # behind it.
    covered: dict[int, float] = {}
    for sle, line, movement, qty in movements:
        if direction != "issue" or movement != "transfer_out":
            continue
        component = int(line.component_item_id)
        if component in covered:
            continue
        warehouse = str(sle.warehouse_ref1c or "")
        # An older issue writer could export a transfer without recording the
        # reservation it was made for at all.  That is a lost record, not a
        # surplus: establish the opening from the physical fact once.
        if not _has_transit_issue_opening(db, issue=issue, line=line):
            if append_material_issue_custody_event(
                db,
                issue=issue,
                line=line,
                delta_qty=outbound_qty[component],
                source_kind="issue_created",
                location_kind="transit",
                warehouse_ref1c=warehouse,
                source_ref1c=source,
                source_ref2c=ref,
                effective_at=sle.posting_at,
            ):
                appended += 1
            db.flush()
        covered[component] = max(
            0.0,
            min(
                outbound_qty[component],
                _custody_balance(
                    db,
                    issue=issue,
                    line=line,
                    location_kind="transit",
                    warehouse_ref1c=warehouse,
                    exclude_recorder_ref=ref,
                ),
            ),
        )
    transit_left = dict(covered)
    workshop_left = dict(covered)

    for sle, line, movement, qty in movements:
        component = int(line.component_item_id)
        if direction == "return":
            if movement != "transfer_out":
                continue
            source_kind, location, delta = "transfer_returned", "workshop", -qty
        elif movement == "transfer_out":
            take = min(qty, transit_left.get(component, 0.0))
            transit_left[component] = transit_left.get(component, 0.0) - take
            if take <= _EPSILON:
                continue
            source_kind, location, delta = "transfer_posted", "transit", -take
        elif movement == "transfer_in":
            take = min(qty, workshop_left.get(component, 0.0))
            workshop_left[component] = workshop_left.get(component, 0.0) - take
            if take <= _EPSILON:
                continue
            source_kind, location, delta = "transfer_posted", "workshop", take
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
