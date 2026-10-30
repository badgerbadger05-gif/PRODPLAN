from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from ..models import (
    ProductionMaterialCustodyEvent,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionProduct,
    LedgerGeneration,
    PlanningTruthState,
    StockLedgerEntry,
    SyncLink,
)
from .production_control_common import to_float as _to_float
from .production_control_common import to_float_strict as _to_float_strict
from .one_c_export_common import clean_ref1c as _clean_ref1c
from .item_ledger.recorder_identity import build_recorder_identity_index

_EPSILON = 1.0e-9
_IDEMPOTENCY_PREFIX = "custody-event"
_STOCK_TRANSFER_ENTITY = "Document_ПеремещениеЗапасов"


@dataclass(frozen=True)
class CandidateCustodyEventSpec:
    """Pure custody evidence prepared from physical facts.

    A spec deliberately has no database event id.  It is safe to build while a
    physical generation is BUILDING: only the final publication boundary may
    materialize it in the append-only canonical stream.
    """

    issue_id: Optional[int]
    product_id: int
    component_item_id: int
    source_kind: str
    source_sle_id: Optional[int]
    source_sle_identity: Optional[str]
    effective_at: datetime
    location_kind: str
    warehouse_ref1c: str
    source_ref1c: Optional[str]
    source_ref2c: Optional[str]
    delta_qty: float
    idempotency_key: str
    document_number: Optional[str]
    document_line_no: Optional[str]
    physical_line_no: Optional[str]
    virtual_ordinal: int = 0
    source_characteristic_ref: Optional[str] = None
    source_organization_ref: Optional[str] = None
    source_record_type: Optional[str] = None
    source_movement_kind: Optional[str] = None

    @property
    def virtual_order_key(self) -> tuple[object, ...]:
        """Deterministic candidate-only ordering; never a global HWM."""
        return (
            self.effective_at,
            0 if self.source_kind == "issue_created" else 1,
            self.virtual_ordinal,
        )


def candidate_custody_spec_to_payload(
    spec: CandidateCustodyEventSpec,
) -> dict[str, object]:
    """Lossless BUILDING metadata; never accepted event provenance."""
    return {
        "issue_id": spec.issue_id,
        "product_id": int(spec.product_id),
        "component_item_id": int(spec.component_item_id),
        "source_kind": spec.source_kind,
        "source_sle_id": spec.source_sle_id,
        "source_sle_identity": spec.source_sle_identity,
        "effective_at": spec.effective_at.isoformat(),
        "location_kind": spec.location_kind,
        "warehouse_ref1c": spec.warehouse_ref1c,
        "source_ref1c": spec.source_ref1c,
        "source_ref2c": spec.source_ref2c,
        "delta_qty": str(Decimal(str(spec.delta_qty))),
        "idempotency_key": spec.idempotency_key,
        "document_number": spec.document_number,
        "document_line_no": spec.document_line_no,
        "physical_line_no": spec.physical_line_no,
        "virtual_ordinal": int(spec.virtual_ordinal),
        "source_characteristic_ref": spec.source_characteristic_ref,
        "source_organization_ref": spec.source_organization_ref,
        "source_record_type": spec.source_record_type,
        "source_movement_kind": spec.source_movement_kind,
    }


def candidate_custody_spec_from_payload(
    value: object,
) -> CandidateCustodyEventSpec:
    if not isinstance(value, dict):
        raise ValueError("candidate custody spec is malformed")
    try:
        effective_at = datetime.fromisoformat(str(value["effective_at"]))
        delta = Decimal(str(value["delta_qty"]))
        if not delta.is_finite():
            raise ValueError
        return CandidateCustodyEventSpec(
            issue_id=(None if value.get("issue_id") is None else int(value["issue_id"])),
            product_id=int(value["product_id"]),
            component_item_id=int(value["component_item_id"]),
            source_kind=str(value["source_kind"]),
            source_sle_id=(None if value.get("source_sle_id") is None else int(value["source_sle_id"])),
            source_sle_identity=(None if value.get("source_sle_identity") is None else str(value["source_sle_identity"])),
            effective_at=effective_at,
            location_kind=str(value["location_kind"]),
            warehouse_ref1c=str(value["warehouse_ref1c"]),
            source_ref1c=(None if value.get("source_ref1c") is None else str(value["source_ref1c"])),
            source_ref2c=(None if value.get("source_ref2c") is None else str(value["source_ref2c"])),
            delta_qty=delta,
            idempotency_key=str(value["idempotency_key"]),
            document_number=(None if value.get("document_number") is None else str(value["document_number"])),
            document_line_no=(None if value.get("document_line_no") is None else str(value["document_line_no"])),
            physical_line_no=(None if value.get("physical_line_no") is None else str(value["physical_line_no"])),
            virtual_ordinal=int(value["virtual_ordinal"]),
            source_characteristic_ref=(None if value.get("source_characteristic_ref") is None else str(value["source_characteristic_ref"])),
            source_organization_ref=(None if value.get("source_organization_ref") is None else str(value["source_organization_ref"])),
            source_record_type=(None if value.get("source_record_type") is None else str(value["source_record_type"])),
            source_movement_kind=(None if value.get("source_movement_kind") is None else str(value["source_movement_kind"])),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError("candidate custody spec is malformed") from exc


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


def invalidate_material_custody_dependents(
    db: Session, *, source_revision: str,
) -> None:
    from .item_ledger.current_execution import invalidate_current_execution_scope

    for entity_kind, scope_key in (
        ("shelf_projection", "shelf:all-live-mrps"),
        ("assembly_readiness", "assembly:all-live-plans"),
        ("drum_schedule", "drum:all-live-plans"),
    ):
        invalidate_current_execution_scope(
            db,
            entity_kind=entity_kind,
            scope_key=scope_key,
            source_revision=source_revision,
            reason="custody_coverage_changed",
        )


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
    invalidate_current: bool = True,
    stamp_accepted_mutation: bool = True,
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

    mutation_boundary = None
    if (
        source_sle_id is None
        and stamp_accepted_mutation
        and db.get(PlanningTruthState, 1) is not None
    ):
        from .accepted_command_mutation import lock_accepted_mutation_state
        mutation_boundary = lock_accepted_mutation_state(db)

    # Serialize before INSERT allocates the custody event id.  Otherwise a
    # second transaction can publish N+1 while N is still uncommitted, causing
    # the compact watermark to leap over the first event.
    from .production_material_custody_projection import lock_current_custody_marker

    lock_current_custody_marker(db)

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
    event = ProductionMaterialCustodyEvent(
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
    db.add(event)
    db.flush()
    from .production_material_custody_projection import apply_local_custody_event_to_current

    apply_local_custody_event_to_current(db, event=event)
    if mutation_boundary is not None:
        from .accepted_command_mutation import record_locked_accepted_production_mutation
        record_locked_accepted_production_mutation(
            db,
            pointer=mutation_boundary[0],
            state=mutation_boundary[1],
            mutation_kind="custody_event",
            product_ids=(int(issue.product_id),),
            item_ids=(int(line.component_item_id),),
        )
    if invalidate_current:
        invalidate_material_custody_dependents(
            db, source_revision=f"custody:event:{int(event.id)}"
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
    terminal_order_ref: Optional[str] = None,
) -> float:
    """What one custody cell holds before a given transfer is folded into it.

    The document's own physical postings are excluded so a replay reads the same
    balance the first run did: without that, re-projecting a recorder would see
    its own consumption and refuse to finish a half-written pair.  Reservations
    of the same document are kept — the opening is exactly what the transfer is
    allowed to consume.

    The order-terminal observer releases custody with ``issue_id=None`` and the
    exact order ref in ``source_ref1c``.  When that exact order ref is
    resolvable for this issue, those terminal releases count against the same
    cell: a transfer re-posted after the order went terminal must not consume
    custody the terminal release already closed.  Without a resolvable order
    ref the lineage cannot be proven and the legacy issue-scoped balance is
    kept — attribution without proof stays fail-closed downstream.
    """
    ownership = or_(
        ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id),
        (
            and_(
                ProductionMaterialCustodyEvent.source_kind == "terminal_release",
                ProductionMaterialCustodyEvent.source_ref1c
                == str(terminal_order_ref),
            )
            if terminal_order_ref
            else False
        ),
    )
    query = db.query(
        func.coalesce(func.sum(ProductionMaterialCustodyEvent.delta_qty), 0)
    ).filter(
        ownership,
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


def _issue_terminal_order_ref(
    db: Session, *, issue: ProductionMaterialIssue
) -> Optional[str]:
    """Exact 1С order ref behind an issue's product, for terminal lineage."""
    order_id = int(getattr(issue, "order_id", 0) or 0)
    if not order_id:
        return None
    order = db.get(ProductionOrder, order_id)
    if order is None:
        return None
    ref = str(getattr(order, "order_ref1c", "") or "").strip()
    return _clean_ref1c(ref) or None


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


def build_transfer_custody_event_specs_for_recorder(
    db: Session,
    *,
    recorder_type: str,
    recorder_ref: str,
    stock_ledger_entries: Iterable[StockLedgerEntry],
) -> tuple[CandidateCustodyEventSpec, ...]:
    """Build transfer custody candidates without append ids or projection DML.

    This is the BUILDING-safe half of the two-stage projector.  Reads use the
    accepted append-only stream as basis and model a missing legacy opening in
    local memory, so the covered quantity is identical to the canonical writer
    without exposing an uncommitted global event watermark.
    """
    if str(recorder_type or "") != _STOCK_TRANSFER_ENTITY:
        return ()
    ref = str(recorder_ref or "").strip()
    if not ref:
        return ()
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
        identity = build_recorder_identity_index(db, [ref])
        order_ids = identity.order_ids.get(ref, set())
        if len(order_ids) != 1:
            return ()
        products = (
            db.query(ProductionProduct)
            .filter(ProductionProduct.order_id == int(next(iter(order_ids))))
            .order_by(ProductionProduct.product_id.asc())
            .all()
        )
        if len(products) != 1:
            return ()
        product = products[0]
        manual: list[CandidateCustodyEventSpec] = []
        for sle in stock_ledger_entries:
            if (
                str(sle.recorder_type or "") != _STOCK_TRANSFER_ENTITY
                or str(sle.recorder_ref or "").strip() != ref
                or not bool(sle.active)
            ):
                raise ValueError("custody projector received an unrelated or inactive SLE row")
            if str(sle.movement_kind or "") != "transfer_in":
                continue
            qty = abs(_to_float_strict(sle.qty, field="ledger_event.qty"))
            warehouse = _clean_ref1c(sle.warehouse_ref1c)
            if qty <= _EPSILON or not warehouse:
                continue
            stable_identity = stable_physical_sle_identity(sle)
            coordinate = stable_identity or f"sle:{int(sle.id)}"
            payload = "|".join((
                "manual-order-transfer", str(int(product.product_id)),
                str(int(sle.item_id)), warehouse, f"{qty:.6f}", coordinate,
            ))
            key = f"{_IDEMPOTENCY_PREFIX}:{hashlib.sha1(payload.encode('utf-8')).hexdigest()}"
            if db.query(ProductionMaterialCustodyEvent.id).filter(
                ProductionMaterialCustodyEvent.idempotency_key == key
            ).first() is not None:
                continue
            manual.append(CandidateCustodyEventSpec(
                issue_id=None, product_id=int(product.product_id),
                component_item_id=int(sle.item_id), source_kind="transfer_posted",
                source_sle_id=int(sle.id), source_sle_identity=stable_identity,
                effective_at=sle.posting_at, location_kind="workshop",
                warehouse_ref1c=warehouse, source_ref1c=None,
                source_ref2c=ref, delta_qty=qty, idempotency_key=key,
                document_number=None, document_line_no=str(sle.line_no or "") or None,
                physical_line_no=str(sle.line_no or "") or None,
                source_characteristic_ref=str(sle.characteristic_ref or ""),
                source_organization_ref=str(sle.organization_ref or ""),
                source_record_type=str(sle.record_type or ""),
                source_movement_kind=str(sle.movement_kind or ""),
            ))
        ordered = sorted(manual, key=lambda spec: (
            spec.effective_at, str(spec.physical_line_no or ""),
            str(spec.source_sle_identity or ""), spec.idempotency_key,
        ))
        return tuple(
            replace(spec, virtual_ordinal=ordinal)
            for ordinal, spec in enumerate(ordered, start=1)
        )
    issue = (
        db.query(ProductionMaterialIssue)
        .options(joinedload(ProductionMaterialIssue.lines))
        .filter(ProductionMaterialIssue.issue_id == int(link.source_id))
        .one_or_none()
    )
    if issue is None:
        return ()
    direction = str(issue.direction or "")
    source = _clean_ref1c(issue.source_warehouse_ref1c)
    destination = _clean_ref1c(issue.warehouse_ref1c)
    if direction not in {"issue", "return"} or (direction == "issue" and source == destination):
        return ()

    lines: dict[int, ProductionMaterialIssueLine] = {}
    for line in sorted(issue.lines or [], key=lambda row: int(row.line_id or 0)):
        component_id = int(line.component_item_id)
        existing = lines.get(component_id)
        if existing is not None and (
            str(existing.unit or "").strip() != str(line.unit or "").strip()
            or int(existing.source_spec_id or 0) != int(line.source_spec_id or 0)
            or str(existing.line_status or "").strip() != str(line.line_status or "").strip()
        ):
            raise RuntimeError(
                "material issue has conflicting duplicate component rows; "
                "custody allocation is ambiguous"
            )
        lines.setdefault(component_id, line)

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

    specs: list[CandidateCustodyEventSpec] = []
    covered: dict[int, float] = {}
    terminal_order_ref = _issue_terminal_order_ref(db, issue=issue) if direction == "issue" else None
    for sle, line, movement, _qty in movements:
        if direction != "issue" or movement != "transfer_out":
            continue
        component = int(line.component_item_id)
        if component in covered:
            continue
        warehouse = _clean_ref1c(sle.warehouse_ref1c)
        if not warehouse:
            continue
        opening_missing = not _has_transit_issue_opening(db, issue=issue, line=line)
        if opening_missing:
            opening_qty = outbound_qty[component]
            opening_key = _custody_event_idempotency_key(
                issue_id=int(issue.issue_id), line_id=int(line.line_id),
                revision=_next_revision(line), source_kind="issue_created",
                location_kind="transit", warehouse_ref1c=warehouse,
                delta_qty=opening_qty, source_sle_id=None,
            )
            specs.append(CandidateCustodyEventSpec(
                issue_id=int(issue.issue_id), product_id=int(issue.product_id),
                component_item_id=component, source_kind="issue_created",
                source_sle_id=None, source_sle_identity=None,
                effective_at=sle.posting_at, location_kind="transit",
                warehouse_ref1c=warehouse, source_ref1c=source,
                source_ref2c=ref, delta_qty=opening_qty,
                idempotency_key=opening_key,
                document_number=str(issue.document_number or ""),
                document_line_no=str(line.line_id),
                physical_line_no=None,
            ))
        basis = _custody_balance(
            db, issue=issue, line=line, location_kind="transit",
            warehouse_ref1c=warehouse, exclude_recorder_ref=ref,
            terminal_order_ref=terminal_order_ref,
        )
        if opening_missing:
            basis += outbound_qty[component]
        covered[component] = max(0.0, min(outbound_qty[component], basis))

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
        warehouse = _clean_ref1c(sle.warehouse_ref1c)
        if not warehouse or _has_equivalent_physical_custody_event(
            db, issue=issue, line=line, source_kind=source_kind,
            location_kind=location, warehouse_ref1c=warehouse, sle=sle,
        ):
            continue
        stable_identity = stable_physical_sle_identity(sle)
        key = _custody_event_idempotency_key(
            issue_id=int(issue.issue_id), line_id=int(line.line_id),
            revision=_next_revision(line), source_kind=source_kind,
            location_kind=location, warehouse_ref1c=warehouse,
            delta_qty=delta, source_sle_id=int(sle.id),
            source_sle_identity=stable_identity,
        )
        specs.append(CandidateCustodyEventSpec(
            issue_id=int(issue.issue_id), product_id=int(issue.product_id),
            component_item_id=component, source_kind=source_kind,
            source_sle_id=int(sle.id), source_sle_identity=stable_identity,
            effective_at=sle.posting_at, location_kind=location,
            warehouse_ref1c=warehouse, source_ref1c=source,
            source_ref2c=ref, delta_qty=delta, idempotency_key=key,
            document_number=str(issue.document_number or ""),
            document_line_no=str(line.line_id),
            physical_line_no=str(sle.line_no or "") or None,
            source_characteristic_ref=str(sle.characteristic_ref or ""),
            source_organization_ref=str(sle.organization_ref or ""),
            source_record_type=str(sle.record_type or ""),
            source_movement_kind=str(sle.movement_kind or ""),
        ))
    # Preserve the established recorder/SLE order (line number, then stable
    # physical identity).  The publication boundary materializes this exact
    # order; it may map ordinals to any fresh id range without changing the
    # persisted event-id semantics used by historical events.
    ordered = sorted(specs, key=lambda spec: (
        spec.effective_at,
        0 if spec.source_kind == "issue_created" else 1,
        str(spec.physical_line_no or ""),
        str(spec.source_sle_identity or ""),
        spec.idempotency_key,
    ))
    return tuple(
        replace(spec, virtual_ordinal=ordinal)
        for ordinal, spec in enumerate(ordered, start=1)
    )


def _require_exact_physical_spec_visibility(
    db: Session,
    spec: CandidateCustodyEventSpec,
    *,
    generation_id: Optional[int],
) -> None:
    """Revalidate one non-duplicate physical spec in a sealed visibility prefix."""
    if spec.source_sle_id is None:
        return
    from .item_ledger.physical_visibility import visible_sle_query

    sle = db.get(StockLedgerEntry, int(spec.source_sle_id))
    if sle is None:
        raise RuntimeError("candidate custody source SLE disappeared before publication")
    generations = []
    if generation_id is not None:
        generation = db.get(LedgerGeneration, int(generation_id))
        if generation is not None:
            generations = [generation]
    else:
        generations = (
            db.query(LedgerGeneration)
            .filter(LedgerGeneration.physical_import_batch_id == int(sle.ingest_batch_id))
            .filter(LedgerGeneration.status.in_(("building", "accepted")))
            .filter(LedgerGeneration.cutoff >= spec.effective_at)
            .order_by(LedgerGeneration.id.desc())
            .all()
        )
        if len(generations) > 1:
            pointer = db.get(PlanningTruthState, 1)
            exact = [row for row in generations if pointer is not None and int(row.id) == int(pointer.current_generation_id or 0)]
            generations = exact
    if len(generations) != 1:
        raise RuntimeError("candidate custody source lacks one exact generation visibility proof")
    generation = generations[0]
    visible = visible_sle_query(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=generation.cutoff,
    ).filter(StockLedgerEntry.id == int(spec.source_sle_id)).one_or_none()
    if visible is None:
        raise RuntimeError("candidate custody source is outside exact generation visibility")
    expected_movement = {
        ("transfer_posted", "transit"): "transfer_out",
        ("transfer_posted", "workshop"): "transfer_in",
        ("transfer_returned", "workshop"): "transfer_out",
    }.get((spec.source_kind, spec.location_kind))
    if (
        stable_physical_sle_identity(visible) != spec.source_sle_identity
        or int(visible.item_id) != int(spec.component_item_id)
        or str(visible.characteristic_ref or "") != str(spec.source_characteristic_ref or "")
        or str(visible.organization_ref or "") != str(spec.source_organization_ref or "")
        or str(visible.warehouse_ref1c or "") != str(spec.warehouse_ref1c or "")
        or str(visible.record_type or "") != str(spec.source_record_type or "")
        or str(visible.movement_kind or "") != str(spec.source_movement_kind or "")
        or (expected_movement is not None and str(visible.movement_kind or "") != expected_movement)
        or visible.posting_at != spec.effective_at
        or abs(_to_float_strict(visible.qty, field="ledger_event.qty")) + _EPSILON
        < abs(_to_float_strict(spec.delta_qty, field="delta_qty"))
    ):
        raise RuntimeError("candidate custody source SLE changed before publication")


def materialize_candidate_custody_event_specs(
    db: Session,
    *,
    specs: Iterable[CandidateCustodyEventSpec],
    invalidate_current: bool = True,
    visibility_generation_id: Optional[int] = None,
) -> int:
    """Append a prepared plan through the existing canonical event writer."""
    specs = tuple(specs)
    for spec in specs:
        if db.query(ProductionMaterialCustodyEvent.id).filter(
            ProductionMaterialCustodyEvent.idempotency_key == spec.idempotency_key
        ).first() is not None:
            continue
        _require_exact_physical_spec_visibility(
            db, spec, generation_id=visibility_generation_id,
        )
    appended = 0
    issue_cache: dict[int, tuple[ProductionMaterialIssue, dict[int, ProductionMaterialIssueLine]]] = {}
    for spec in sorted(specs, key=lambda row: row.virtual_ordinal):
        if db.query(ProductionMaterialCustodyEvent.id).filter(
            ProductionMaterialCustodyEvent.idempotency_key == spec.idempotency_key
        ).first() is not None:
            continue
        if spec.issue_id is None:
            db.add(ProductionMaterialCustodyEvent(
                issue_id=None, product_id=spec.product_id,
                component_item_id=spec.component_item_id,
                source_kind=spec.source_kind, source_sle_id=spec.source_sle_id,
                effective_at=spec.effective_at, location_kind=spec.location_kind,
                warehouse_ref1c=spec.warehouse_ref1c,
                source_ref1c=spec.source_ref1c, source_ref2c=spec.source_ref2c,
                delta_qty=spec.delta_qty, idempotency_key=spec.idempotency_key,
                document_number=spec.document_number,
                document_line_no=spec.document_line_no,
            ))
            appended += 1
            continue
        cached = issue_cache.get(int(spec.issue_id))
        if cached is None:
            issue = (
                db.query(ProductionMaterialIssue)
                .options(joinedload(ProductionMaterialIssue.lines))
                .filter(ProductionMaterialIssue.issue_id == int(spec.issue_id))
                .one()
            )
            by_component: dict[int, ProductionMaterialIssueLine] = {}
            for line in sorted(issue.lines or [], key=lambda row: int(row.line_id or 0)):
                by_component.setdefault(int(line.component_item_id), line)
            cached = (issue, by_component)
            issue_cache[int(spec.issue_id)] = cached
        issue, by_component = cached
        line = by_component.get(int(spec.component_item_id))
        if line is None:
            raise RuntimeError("candidate custody component no longer belongs to its issue")
        if spec.source_kind == "issue_created":
            current_key = _custody_event_idempotency_key(
                issue_id=int(issue.issue_id), line_id=int(line.line_id),
                revision=_next_revision(line), source_kind=spec.source_kind,
                location_kind=spec.location_kind,
                warehouse_ref1c=spec.warehouse_ref1c,
                delta_qty=spec.delta_qty, source_sle_id=None,
            )
            if current_key != spec.idempotency_key:
                raise RuntimeError("candidate custody opening revision changed before publication")
        appended += int(append_material_issue_custody_event(
            db, issue=issue, line=line, delta_qty=spec.delta_qty,
            source_kind=spec.source_kind, location_kind=spec.location_kind,
            warehouse_ref1c=spec.warehouse_ref1c,
            source_ref1c=spec.source_ref1c, source_ref2c=spec.source_ref2c,
            effective_at=spec.effective_at, source_sle_id=spec.source_sle_id,
            source_sle_identity=spec.source_sle_identity,
            invalidate_current=invalidate_current,
            stamp_accepted_mutation=False,
        ))
    if appended and any(spec.issue_id is None for spec in specs) and invalidate_current:
        invalidate_material_custody_dependents(
            db, source_revision=f"custody:transfer:{next(spec.source_ref2c for spec in specs if spec.issue_id is None)}"
        )
    db.flush()
    return appended


def project_transfer_custody_events_for_recorder(
    db: Session,
    *,
    recorder_type: str,
    recorder_ref: str,
    stock_ledger_entries: Iterable[StockLedgerEntry],
    invalidate_current: bool = True,
) -> int:
    """Append custody changes only from freshly persisted physical SLE facts."""
    specs = build_transfer_custody_event_specs_for_recorder(
        db, recorder_type=recorder_type, recorder_ref=recorder_ref,
        stock_ledger_entries=stock_ledger_entries,
    )
    return materialize_candidate_custody_event_specs(
        db, specs=specs, invalidate_current=invalidate_current,
    )
