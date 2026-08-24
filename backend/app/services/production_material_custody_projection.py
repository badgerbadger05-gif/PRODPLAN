"""Append-only custody events and projected fold for planning material coverage."""
from __future__ import annotations

import logging

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import models
from .production_control_common import to_float as _to_float
from .production_material_custody import MaterialCustodyState, ProductMaterialCustody
from .production_material_custody_events import stable_physical_sle_identity
from .item_ledger.physical_visibility import visible_sle_query


logger = logging.getLogger(__name__)

_EPSILON = 1.0e-9


def _same_1c_timestamp(left: datetime, right: datetime) -> bool:
    """Compare one 1C wall-clock value across mixed PostgreSQL timestamp types."""
    if left.tzinfo is None or right.tzinfo is None:
        return left.replace(tzinfo=None) == right.replace(tzinfo=None)
    return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)


class MaterialCustodySnapshotUnavailable(RuntimeError):
    def __init__(
        self,
        *,
        product_id: int | None = None,
        component_item_id: int | None = None,
        manifest_generation_id: int | None = None,
        expected_generation_id: int,
        stored_generation_id: int | None,
        reason: str,
    ) -> None:
        self.detail = {
            "code": "material_custody_snapshot_unavailable",
            "status": "unavailable",
            "product_id": None if product_id is None else int(product_id),
            "component_item_id": None if component_item_id is None else int(component_item_id),
            "manifest_generation_id": None if manifest_generation_id is None else int(manifest_generation_id),
            "expected_generation_id": int(expected_generation_id),
            "stored_generation_id": stored_generation_id,
            "reason": reason,
        }
        super().__init__(self.detail["reason"])

    def as_dict(self) -> dict[str, object]:
        return dict(self.detail)


_LOCATION_TRANSIT = "transit"
_LOCATION_WORKSHOP = "workshop"
ProjectionRowKey = Tuple[int, int, str, str]


def _ensure_material_custody_product_state(
    state: MaterialCustodyState,
    product_id: int,
) -> ProductMaterialCustody:
    return state.by_product.setdefault(int(product_id), ProductMaterialCustody())


def _state_from_projection_rows(
    rows: Iterable[models.ProductionMaterialCustodyProjection],
) -> MaterialCustodyState:
    state = MaterialCustodyState()
    for row in rows:
        product_id = int(row.product_id)
        comp_id = int(row.component_item_id)
        warehouse = str(row.warehouse_ref1c or "")
        qty = _to_float(row.reserved_qty)
        if qty <= 0:
            continue

        item = _ensure_material_custody_product_state(state, product_id)
        if str(row.location_kind) == _LOCATION_TRANSIT:
            item.in_transit[comp_id] = item.in_transit.get(comp_id, 0.0) + qty
        else:
            item.at_workshop[comp_id] = item.at_workshop.get(comp_id, 0.0) + qty

        if warehouse:
            state.by_warehouse_item[(warehouse, comp_id)] = (
                state.by_warehouse_item.get((warehouse, comp_id), 0.0) + qty
            )
    return state


def _projection_row_key(row: models.ProductionMaterialCustodyProjection) -> ProjectionRowKey:
    return (
        int(row.product_id),
        int(row.component_item_id),
        str(row.location_kind),
        str(row.warehouse_ref1c or ""),
    )


def _projection_rows_by_key(
    rows: Iterable[models.ProductionMaterialCustodyProjection],
) -> dict[ProjectionRowKey, float]:
    values: dict[ProjectionRowKey, float] = {}
    for row in rows:
        qty = _to_float(row.reserved_qty)
        if qty <= _EPSILON:
            continue

        key = _projection_row_key(row)
        if key in values:
            values[key] += qty
            continue
        values[key] = qty

    return {
        key: value
        for key, value in values.items()
        if value > _EPSILON
    }


def _read_manifest(
    db: Session,
    *,
    generation_id: int,
) -> models.ProductionMaterialCustodyProjectionManifest | None:
    return db.get(models.ProductionMaterialCustodyProjectionManifest, int(generation_id))


def _require_manifest_cutoff(
    manifest: models.ProductionMaterialCustodyProjectionManifest,
    generation: models.LedgerGeneration,
) -> None:
    if manifest.cutoff != generation.cutoff:
        raise MaterialCustodySnapshotUnavailable(
            manifest_generation_id=int(manifest.ledger_generation_id),
            expected_generation_id=int(generation.id),
            stored_generation_id=int(manifest.ledger_generation_id),
            reason="custody snapshot manifest cutoff mismatches Ledger generation",
        )


def _projection_baseline_candidates(
    db: Session,
    *,
    cutoff: datetime,
    current_generation_id: int,
) -> list[int]:
    """Completed manifests a fold could start from, newest first."""
    rows = (
        db.query(models.ProductionMaterialCustodyProjectionManifest.ledger_generation_id)
        .join(
            models.LedgerGeneration,
            models.ProductionMaterialCustodyProjectionManifest.ledger_generation_id
            == models.LedgerGeneration.id,
        )
        .filter(models.ProductionMaterialCustodyProjectionManifest.status == "complete")
        .filter(
            models.ProductionMaterialCustodyProjectionManifest.ledger_generation_id
            != current_generation_id
        )
        .filter(models.ProductionMaterialCustodyProjectionManifest.cutoff <= cutoff)
        .order_by(
            models.LedgerGeneration.cutoff.desc(),
            models.ProductionMaterialCustodyProjectionManifest.ledger_generation_id.desc(),
        )
        .all()
    )
    return [int(row[0]) for row in rows]


def _latest_projection_manifest(
    db: Session,
    *,
    cutoff: datetime,
    current_generation_id: int,
) -> int | None:
    """Latest completed manifest before the target generation cutoff."""
    candidates = _projection_baseline_candidates(
        db, cutoff=cutoff, current_generation_id=current_generation_id
    )
    return candidates[0] if candidates else None


def _event_high_watermark_id_at_cutoff(
    db: Session,
    *,
    cutoff: datetime,
) -> int:
    watermark = (
        db.query(func.max(models.ProductionMaterialCustodyEvent.id))
        .filter(models.ProductionMaterialCustodyEvent.effective_at <= cutoff)
        .scalar()
    )
    return int(watermark or 0)


def _is_reimport_duplicate_physical_event(
    db: Session,
    event: models.ProductionMaterialCustodyEvent,
    *,
    original_high_watermark_id: int,
) -> bool:
    """True only when an older custody event names the same physical SLE line."""
    if event.source_sle_id is None:
        return False
    source = db.get(models.StockLedgerEntry, int(event.source_sle_id))
    if source is None:
        return False
    identity = stable_physical_sle_identity(source)
    if identity is None:
        return False
    content_hash, recorder_type, recorder_ref, line_no = identity.split("|", 3)
    duplicate = (
        db.query(models.ProductionMaterialCustodyEvent.id)
        .join(
            models.StockLedgerEntry,
            models.ProductionMaterialCustodyEvent.source_sle_id
            == models.StockLedgerEntry.id,
        )
        .filter(models.ProductionMaterialCustodyEvent.id <= int(original_high_watermark_id))
        .filter(models.ProductionMaterialCustodyEvent.issue_id == event.issue_id)
        .filter(
            models.ProductionMaterialCustodyEvent.component_item_id
            == event.component_item_id
        )
        .filter(models.ProductionMaterialCustodyEvent.source_kind == event.source_kind)
        .filter(models.ProductionMaterialCustodyEvent.location_kind == event.location_kind)
        .filter(models.ProductionMaterialCustodyEvent.warehouse_ref1c == event.warehouse_ref1c)
        .filter(models.ProductionMaterialCustodyEvent.delta_qty == event.delta_qty)
        .filter(models.StockLedgerEntry.source_content_hash == content_hash)
        .filter(models.StockLedgerEntry.recorder_type == recorder_type)
        .filter(models.StockLedgerEntry.recorder_ref == recorder_ref)
        .filter(models.StockLedgerEntry.line_no == line_no)
        .first()
    )
    return duplicate is not None


def _visible_source_sle_for_event(
    db: Session,
    *,
    event: models.ProductionMaterialCustodyEvent,
    physical_import_batch_id: int,
    cutoff: datetime,
) -> models.StockLedgerEntry | None:
    """Resolve an event's visible physical fact, following only exact reimports."""
    if event.source_sle_id is None:
        return None
    visible = visible_sle_query(
        db,
        physical_import_batch_id=physical_import_batch_id,
        cutoff=cutoff,
    )
    direct = visible.filter(models.StockLedgerEntry.id == int(event.source_sle_id)).one_or_none()
    if direct is not None:
        return direct
    original = db.get(models.StockLedgerEntry, int(event.source_sle_id))
    if original is None:
        return None
    identity = stable_physical_sle_identity(original)
    if identity is None:
        return None
    content_hash, recorder_type, recorder_ref, line_no = identity.split("|", 3)
    candidates = (
        visible.filter(models.StockLedgerEntry.source_content_hash == content_hash)
        .filter(models.StockLedgerEntry.recorder_type == recorder_type)
        .filter(models.StockLedgerEntry.recorder_ref == recorder_ref)
        .filter(models.StockLedgerEntry.line_no == line_no)
        .all()
    )
    return candidates[0] if len(candidates) == 1 else None


def _custody_fold_anchor(
    events: Sequence[models.ProductionMaterialCustodyEvent],
) -> dict[tuple[int, int], datetime]:
    """Одна временная точка на строку заявки: самое раннее её событие.

    Резерв и его расход живут по разным часам. Открытие заявки штампуется
    временем PRODPLAN с микросекундами, а проводка перемещения приходит из 1С —
    целыми секундами и по её часам, которые могут отставать: на стенде заявка
    создана в 10:39:01.07, а документ проведён в 10:39:00. Сравнение по времени
    в такой паре всегда проиграет, на какой бы точности его ни вести.

    Поэтому строка заявки сортируется как целое: все её события встают в поток
    по самому раннему из них, а внутри — по причинности. Между разными строками
    порядок остаётся временным.
    """
    anchors: dict[tuple[int, int], datetime] = {}
    for event in events:
        if event.issue_id is None or event.effective_at is None:
            continue
        key = (int(event.issue_id), int(event.component_item_id))
        moment = event.effective_at.replace(microsecond=0)
        current = anchors.get(key)
        if current is None or moment < current:
            anchors[key] = moment
    return anchors


def _custody_fold_order_key(
    event: models.ProductionMaterialCustodyEvent,
    anchors: Mapping[tuple[int, int], datetime],
) -> tuple[Any, ...]:
    """Порядок свёртки: строка заявки целиком, внутри — открытие перед расходом.

    Открытие резерва всегда предшествует перемещению, которое этот резерв
    тратит: документ не может появиться раньше заявки, из которой он выгружен.
    Раньше порядок определялся временем, и расход вставал перед резервом —
    свёртка уходила в минус, проекция падала закрыто и уносила с собой всю
    сборку поколения Ledger.
    """
    effective = event.effective_at
    truncated = (
        effective.replace(microsecond=0) if effective is not None else effective
    )
    anchor = truncated
    if event.issue_id is not None and effective is not None:
        anchor = anchors.get(
            (int(event.issue_id), int(event.component_item_id)), truncated
        )
    return (
        anchor,
        0 if str(event.source_kind or "") == "issue_created" else 1,
        truncated,
        effective,
        int(event.id),
    )


def _late_events_behind_baseline(
    db: Session,
    *,
    baseline_cutoff: datetime,
    baseline_high_watermark_id: int,
    target_high_watermark_id: int,
) -> list[int]:
    """Events appended after a baseline was built, yet dated inside its window.

    Such an event is invisible to an incremental fold: the fold replays only what
    is dated *after* the baseline cutoff, so a movement discovered later but
    posted earlier would silently never be applied.  Ordinary operation produces
    them all the time — a recorder re-pull projects custody for a document posted
    days ago, a backdated transfer arrives, a repair appends a missing
    reservation — so this is a signal to fold from an older baseline, not a
    defect.  Only a re-import of a physical line an older event already carries
    is exempt: it changes nothing.
    """
    return [
        int(event_id)
        for (event_id,) in (
            db.query(models.ProductionMaterialCustodyEvent.id)
            .filter(
                models.ProductionMaterialCustodyEvent.id > int(baseline_high_watermark_id)
            )
            .filter(
                models.ProductionMaterialCustodyEvent.id <= int(target_high_watermark_id)
            )
            .filter(
                models.ProductionMaterialCustodyEvent.effective_at <= baseline_cutoff
            )
            .all()
        )
        if not _is_reimport_duplicate_physical_event(
            db,
            db.get(models.ProductionMaterialCustodyEvent, int(event_id)),
            original_high_watermark_id=int(baseline_high_watermark_id),
        )
    ]


def _resolve_projection_baseline(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    target_high_watermark_id: int,
) -> tuple[int, models.ProductionMaterialCustodyProjectionManifest, models.LedgerGeneration]:
    """Newest baseline whose own window no later append has changed.

    Starting from the newest manifest is only correct while every event dated
    inside it was already counted in it.  When a later append breaks that — the
    normal case for a document projected long after it was posted — the fold
    rewinds to an older baseline instead of failing closed.  That failure is what
    stopped the Ledger from refreshing at all: nothing in the pipeline could
    produce a new generation, so the cutoff froze and planning truth went stale
    behind it.

    Rewinding is safe because a baseline is a full projection, not a delta: an
    older one simply means replaying more events.  If even the oldest manifest is
    behind such an event, the event predates the explicit cutover baseline and no
    fold can place it — that is a real break, and it fails closed.
    """
    candidates = _projection_baseline_candidates(
        db,
        cutoff=generation.cutoff,
        current_generation_id=int(generation.id),
    )
    if not candidates:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=None,
            reason=(
                "custody snapshot baseline is missing; no completed manifest exists "
                "for earlier Ledger generation"
            ),
        )
    rewound_past: list[int] = []
    for baseline_generation_id in candidates:
        manifest = _read_manifest(db, generation_id=baseline_generation_id)
        if manifest is None:
            raise MaterialCustodySnapshotUnavailable(
                manifest_generation_id=baseline_generation_id,
                expected_generation_id=int(generation.id),
                stored_generation_id=None,
                reason="custody snapshot baseline manifest record is missing",
            )
        baseline_generation = db.get(models.LedgerGeneration, baseline_generation_id)
        if baseline_generation is None:
            raise MaterialCustodySnapshotUnavailable(
                manifest_generation_id=baseline_generation_id,
                expected_generation_id=int(generation.id),
                stored_generation_id=baseline_generation_id,
                reason="custody snapshot baseline Ledger generation is missing",
            )
        _require_manifest_cutoff(manifest, baseline_generation)
        watermark = int(manifest.source_event_high_watermark_id)
        if watermark > int(target_high_watermark_id):
            rewound_past.append(baseline_generation_id)
            continue
        if _late_events_behind_baseline(
            db,
            baseline_cutoff=baseline_generation.cutoff,
            baseline_high_watermark_id=watermark,
            target_high_watermark_id=int(target_high_watermark_id),
        ):
            rewound_past.append(baseline_generation_id)
            continue
        if rewound_past:
            logger.info(
                "custody projection for generation %s rewound past baseline(s) %s "
                "to %s: later appends are dated inside their windows",
                int(generation.id),
                ", ".join(str(value) for value in rewound_past),
                baseline_generation_id,
            )
        return baseline_generation_id, manifest, baseline_generation
    raise MaterialCustodySnapshotUnavailable(
        expected_generation_id=int(generation.id),
        stored_generation_id=candidates[-1],
        reason=(
            "custody events were appended behind every available baseline "
            "(checked " + ", ".join(str(value) for value in candidates) + "); "
            "they predate the explicit cutover and no fold can place them"
        ),
    )


def _select_visible_custody_events(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    baseline_cutoff: datetime,
    baseline_high_watermark_id: int,
    target_high_watermark_id: int,
) -> Sequence[models.ProductionMaterialCustodyEvent]:
    if baseline_cutoff is None:
        raise MaterialCustodySnapshotUnavailable(
            manifest_generation_id=int(generation.id),
            expected_generation_id=int(generation.id),
            stored_generation_id=int(generation.id),
            reason="custody snapshot baseline cutoff is missing",
        )

    if target_high_watermark_id < baseline_high_watermark_id:
        raise MaterialCustodySnapshotUnavailable(
            manifest_generation_id=int(generation.id),
            expected_generation_id=int(generation.id),
            stored_generation_id=int(generation.id),
            reason="custody snapshot target watermark cannot be older than baseline watermark",
        )

    if _late_events_behind_baseline(
        db,
        baseline_cutoff=baseline_cutoff,
        baseline_high_watermark_id=baseline_high_watermark_id,
        target_high_watermark_id=target_high_watermark_id,
    ):
        raise MaterialCustodySnapshotUnavailable(
            manifest_generation_id=int(generation.id),
            expected_generation_id=int(generation.id),
            stored_generation_id=int(generation.id),
            reason=(
                "custody event appears after baseline watermark but is not "
                "strictly after baseline cutoff"
            ),
        )

    visible_sle_ids = visible_sle_query(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=generation.cutoff,
    ).with_entities(models.StockLedgerEntry.id)

    events = (
        db.query(models.ProductionMaterialCustodyEvent)
        .filter(models.ProductionMaterialCustodyEvent.effective_at > baseline_cutoff)
        .filter(models.ProductionMaterialCustodyEvent.effective_at <= generation.cutoff)
        .filter(models.ProductionMaterialCustodyEvent.id <= target_high_watermark_id)
        .filter(
            or_(
                models.ProductionMaterialCustodyEvent.source_sle_id.is_(None),
                models.ProductionMaterialCustodyEvent.source_sle_id.in_(visible_sle_ids),
            )
        )
        # Fold order is owned by ``_custody_fold_order_key`` alone; this is only
        # a stable read order, never a second ordering rule.
        .order_by(models.ProductionMaterialCustodyEvent.id.asc())
        .all()
    )
    visible = [
        event
        for event in events
        if not _is_reimport_duplicate_physical_event(
            db,
            event,
            original_high_watermark_id=baseline_high_watermark_id,
        )
    ]
    anchors = _custody_fold_anchor(visible)
    return sorted(visible, key=lambda event: _custody_fold_order_key(event, anchors))


def initialize_material_custody_baseline(
    db: Session,
    *,
    ledger_generation_id: int,
    cells: Sequence[dict[str, Any]],
    observed_at: datetime,
) -> dict[str, Any]:
    """Persist one explicit cutover baseline; never infer it from live issues."""
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if (
        generation is None
        or generation.cutoff is None
        or str(generation.status or "") != "building"
    ):
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(ledger_generation_id),
            stored_generation_id=None,
            reason="custody baseline requires a BUILDING Ledger generation with cutoff",
        )
    if observed_at != generation.cutoff:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=None,
            reason="custody baseline observed_at must equal Ledger generation cutoff",
        )
    if _read_manifest(db, generation_id=int(generation.id)) is not None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=int(generation.id),
            reason="custody baseline manifest already exists",
        )

    watermark = _event_high_watermark_id_at_cutoff(db, cutoff=generation.cutoff)
    rows: dict[ProjectionRowKey, float] = {}
    for cell in cells:
        try:
            product_id = int(cell["product_id"])
            component_item_id = int(cell["component_item_id"])
            location = str(cell["location_kind"])
            warehouse = str(cell["warehouse_ref1c"])
            value = _to_float(cell["reserved_qty"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MaterialCustodySnapshotUnavailable(
                expected_generation_id=int(generation.id),
                stored_generation_id=None,
                reason="custody baseline contains a malformed explicit cell",
            ) from exc
        if location not in {_LOCATION_TRANSIT, _LOCATION_WORKSHOP} or not warehouse or value <= _EPSILON:
            raise MaterialCustodySnapshotUnavailable(
                product_id=product_id,
                component_item_id=component_item_id,
                expected_generation_id=int(generation.id),
                stored_generation_id=None,
                reason="custody baseline contains an invalid explicit cell",
            )
        key = (product_id, component_item_id, location, warehouse)
        if key in rows:
            raise MaterialCustodySnapshotUnavailable(
                product_id=product_id,
                component_item_id=component_item_id,
                expected_generation_id=int(generation.id),
                stored_generation_id=None,
                reason="custody baseline contains a duplicate explicit cell",
            )
        rows[key] = value

    now = datetime.now(timezone.utc)
    for (product_id, component_item_id, location, warehouse), qty in sorted(rows.items()):
        db.add(models.ProductionMaterialCustodyProjection(
            ledger_generation_id=int(generation.id),
            product_id=product_id,
            component_item_id=component_item_id,
            location_kind=location,
            warehouse_ref1c=warehouse,
            reserved_qty=qty,
            source_event_high_watermark_id=watermark,
            built_at=now,
        ))
    db.add(models.ProductionMaterialCustodyProjectionManifest(
        ledger_generation_id=int(generation.id),
        baseline_generation_id=int(generation.id),
        cutoff=generation.cutoff,
        status="complete",
        is_baseline=True,
        source_event_high_watermark_id=watermark,
        observed_at=observed_at,
        built_at=now,
    ))
    db.flush()
    return {
        "ledger_generation_id": int(generation.id),
        "source_event_high_watermark_id": watermark,
        "projection_rows": len(rows),
        "baseline": True,
    }


def _build_projection_from_seed_and_events(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    baseline_generation_id: int,
    baseline_high_watermark_id: int,
    baseline_cutoff: datetime,
    target_high_watermark_id: int,
) -> tuple[MaterialCustodyState, dict[ProjectionRowKey, float]]:
    baseline_rows = (
        db.query(models.ProductionMaterialCustodyProjection)
        .filter_by(ledger_generation_id=int(baseline_generation_id))
        .all()
    )
    state = _state_from_projection_rows(baseline_rows)
    projection_rows = _projection_rows_by_key(baseline_rows)

    events = _select_visible_custody_events(
        db,
        generation=generation,
        baseline_cutoff=baseline_cutoff,
        baseline_high_watermark_id=baseline_high_watermark_id,
        target_high_watermark_id=target_high_watermark_id,
    )

    for event in events:
        product = _ensure_material_custody_product_state(
            state,
            int(event.product_id),
        )
        comp_id = int(event.component_item_id)
        warehouse = str(event.warehouse_ref1c or "")
        delta = _to_float(event.delta_qty)
        location = str(event.location_kind)

        physical_kind = str(event.source_kind) in {
            "transfer_posted", "transfer_returned", "consumed"
        }
        if physical_kind and event.source_sle_id is None:
            raise MaterialCustodySnapshotUnavailable(
                product_id=int(event.product_id),
                component_item_id=comp_id,
                manifest_generation_id=int(generation.id),
                expected_generation_id=int(generation.id),
                stored_generation_id=int(baseline_generation_id),
                reason="physical custody event has no source SLE",
            )
        if event.source_sle_id is not None:
            sle = _visible_source_sle_for_event(
                db,
                event=event,
                physical_import_batch_id=int(generation.physical_import_batch_id),
                cutoff=generation.cutoff,
            )
            if (
                sle is None
                or int(sle.item_id) != comp_id
                or str(sle.warehouse_ref1c or "") != warehouse
                or not _same_1c_timestamp(sle.posting_at, event.effective_at)
                or abs(_to_float(sle.qty)) + _EPSILON < abs(delta)
            ):
                raise MaterialCustodySnapshotUnavailable(
                    product_id=int(event.product_id),
                    component_item_id=comp_id,
                    manifest_generation_id=int(generation.id),
                    expected_generation_id=int(generation.id),
                    stored_generation_id=int(baseline_generation_id),
                    reason="custody event source SLE is not visible or mismatches its cell",
                )

        key = (int(event.product_id), comp_id, location, warehouse)
        current_qty = projection_rows.get(key, 0.0)
        next_qty = current_qty + delta
        if next_qty < -_EPSILON:
            raise MaterialCustodySnapshotUnavailable(
                product_id=int(event.product_id),
                component_item_id=comp_id,
                manifest_generation_id=int(generation.id),
                expected_generation_id=int(generation.id),
                stored_generation_id=int(baseline_generation_id),
                reason=f"custody event fold produced negative {location} reservation",
            )

        if next_qty <= _EPSILON:
            projection_rows.pop(key, None)
        else:
            projection_rows[key] = next_qty

        if location == _LOCATION_TRANSIT:
            next_in_transit = product.in_transit.get(comp_id, 0.0) + delta
            if next_in_transit < -_EPSILON:
                raise MaterialCustodySnapshotUnavailable(
                    product_id=int(event.product_id),
                    component_item_id=comp_id,
                    manifest_generation_id=int(generation.id),
                    expected_generation_id=int(generation.id),
                    stored_generation_id=int(baseline_generation_id),
                    reason="custody event fold produced negative in-transit reservation",
                )
            if next_in_transit <= _EPSILON:
                product.in_transit.pop(comp_id, None)
            else:
                product.in_transit[comp_id] = next_in_transit
        elif location == _LOCATION_WORKSHOP:
            next_at_workshop = product.at_workshop.get(comp_id, 0.0) + delta
            if next_at_workshop < -_EPSILON:
                raise MaterialCustodySnapshotUnavailable(
                    product_id=int(event.product_id),
                    component_item_id=comp_id,
                    manifest_generation_id=int(generation.id),
                    expected_generation_id=int(generation.id),
                    stored_generation_id=int(baseline_generation_id),
                    reason="custody event fold produced negative workshop reservation",
                )
            if next_at_workshop <= _EPSILON:
                product.at_workshop.pop(comp_id, None)
            else:
                product.at_workshop[comp_id] = next_at_workshop
        else:
            raise MaterialCustodySnapshotUnavailable(
                product_id=int(event.product_id),
                component_item_id=comp_id,
                manifest_generation_id=int(generation.id),
                expected_generation_id=int(generation.id),
                stored_generation_id=int(baseline_generation_id),
                reason="unsupported custody event location",
            )

        warehouse_qty = state.by_warehouse_item.get((warehouse, comp_id), 0.0) + delta
        if warehouse_qty <= _EPSILON:
            state.by_warehouse_item.pop((warehouse, comp_id), None)
        else:
            state.by_warehouse_item[(warehouse, comp_id)] = warehouse_qty

    return state, projection_rows


def build_material_custody_projection(
    db: Session,
    *,
    ledger_generation_id: int,
) -> dict[str, Any]:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(ledger_generation_id),
            stored_generation_id=None,
            reason="target Ledger generation is missing",
        )
    if generation.cutoff is None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=None,
            reason="target Ledger generation has no cutoff",
        )
    if str(generation.status or "") != "building":
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=None,
            reason="material custody projection requires a BUILDING generation",
        )

    target_high_watermark_id = _event_high_watermark_id_at_cutoff(
        db,
        cutoff=generation.cutoff,
    )

    manifest = _read_manifest(db, generation_id=int(generation.id))
    existing_rows = (
        db.query(models.ProductionMaterialCustodyProjection)
        .filter_by(ledger_generation_id=int(generation.id))
        .all()
    )
    if manifest is not None:
        _require_manifest_cutoff(manifest, generation)
        if str(manifest.status) != "complete":
            raise MaterialCustodySnapshotUnavailable(
                expected_generation_id=int(generation.id),
                stored_generation_id=int(generation.id),
                reason="custody snapshot manifest already exists but is incomplete",
            )
        if int(manifest.source_event_high_watermark_id) != int(target_high_watermark_id):
            raise MaterialCustodySnapshotUnavailable(
                expected_generation_id=int(generation.id),
                stored_generation_id=int(generation.id),
                reason="custody snapshot manifest conflicts with current event watermark",
            )
        existing_counts = {
            int(row.source_event_high_watermark_id)
            for row in existing_rows
            if row.source_event_high_watermark_id is not None
        }
        if len(existing_counts) > 1:
            raise MaterialCustodySnapshotUnavailable(
                expected_generation_id=int(generation.id),
                stored_generation_id=int(generation.id),
                reason="custody snapshot row provenance is malformed",
            )
        if existing_counts and next(iter(existing_counts)) != int(target_high_watermark_id):
            raise MaterialCustodySnapshotUnavailable(
                expected_generation_id=int(generation.id),
                stored_generation_id=int(generation.id),
                reason="custody snapshot row watermark mismatches generation manifest",
            )
        validate_material_custody_projection(
            db, ledger_generation_id=int(generation.id)
        )
        return {
            "ledger_generation_id": int(generation.id),
            "baseline_generation_id": (
                int(manifest.baseline_generation_id)
                if manifest.baseline_generation_id is not None
                else None
            ),
            "source_event_high_watermark_id": int(target_high_watermark_id),
            "projection_rows": len(existing_rows),
            "manifest_id": int(manifest.ledger_generation_id),
            "valid": True,
        }

    if existing_rows:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=int(generation.id),
            reason="custody projection rows exist without a manifest",
        )

    baseline_generation_id, baseline_manifest, baseline_generation = (
        _resolve_projection_baseline(
            db,
            generation=generation,
            target_high_watermark_id=target_high_watermark_id,
        )
    )
    baseline_high_watermark_id = int(baseline_manifest.source_event_high_watermark_id)

    _, target_rows = _build_projection_from_seed_and_events(
        db,
        generation=generation,
        baseline_generation_id=baseline_generation_id,
        baseline_high_watermark_id=baseline_high_watermark_id,
        baseline_cutoff=baseline_generation.cutoff,
        target_high_watermark_id=target_high_watermark_id,
    )

    for product_id, comp_id, location, warehouse in sorted(target_rows):
        reserved_qty = _to_float(target_rows[(product_id, comp_id, location, warehouse)])
        if reserved_qty <= _EPSILON:
            continue
        db.add(
            models.ProductionMaterialCustodyProjection(
                ledger_generation_id=int(generation.id),
                product_id=product_id,
                component_item_id=comp_id,
                location_kind=location,
                warehouse_ref1c=warehouse,
                reserved_qty=reserved_qty,
                source_event_high_watermark_id=int(target_high_watermark_id),
            )
        )

    now = datetime.now(timezone.utc)
    manifest = models.ProductionMaterialCustodyProjectionManifest(
        ledger_generation_id=int(generation.id),
        baseline_generation_id=int(baseline_generation_id),
        cutoff=generation.cutoff,
        status="complete",
        is_baseline=False,
        source_event_high_watermark_id=int(target_high_watermark_id),
        observed_at=now,
        built_at=now,
    )
    db.add(manifest)
    db.flush()

    return {
        "ledger_generation_id": int(generation.id),
        "baseline_generation_id": int(baseline_generation_id),
        "source_event_high_watermark_id": int(target_high_watermark_id),
        "projection_rows": len(target_rows),
        "manifest_id": int(manifest.ledger_generation_id),
        "valid": True,
    }


def validate_material_custody_projection(
    db: Session,
    *,
    ledger_generation_id: int,
) -> dict[str, Any]:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(ledger_generation_id),
            stored_generation_id=None,
            reason="target Ledger generation is missing",
        )
    if generation.cutoff is None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=None,
            reason="target Ledger generation has no cutoff",
        )

    manifest = _read_manifest(db, generation_id=int(generation.id))
    if manifest is None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=None,
            reason="custody snapshot manifest is missing",
        )
    if str(manifest.status) != "complete":
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=int(manifest.ledger_generation_id),
            reason="custody snapshot manifest is not complete",
        )
    _require_manifest_cutoff(manifest, generation)

    observed_watermark = _event_high_watermark_id_at_cutoff(
        db,
        cutoff=generation.cutoff,
    )
    target_watermark = int(manifest.source_event_high_watermark_id)
    if target_watermark != observed_watermark:
        raise MaterialCustodySnapshotUnavailable(
            manifest_generation_id=int(manifest.ledger_generation_id),
            expected_generation_id=int(generation.id),
            stored_generation_id=int(manifest.ledger_generation_id),
            reason="custody snapshot manifest watermark mismatches visible custody events",
        )

    rows = (
        db.query(models.ProductionMaterialCustodyProjection)
        .filter_by(ledger_generation_id=int(generation.id))
        .all()
    )
    distinct_counts = {
        int(row.source_event_high_watermark_id)
        for row in rows
        if row.source_event_high_watermark_id is not None
    }
    if len(distinct_counts) > 1:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=int(generation.id),
            reason="custody snapshot row provenance is malformed",
        )
    if distinct_counts and next(iter(distinct_counts)) != target_watermark:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=int(generation.id),
            reason="custody snapshot row watermark mismatches generation manifest",
        )

    if bool(manifest.is_baseline):
        return {
            "ledger_generation_id": int(generation.id),
            "baseline_generation_id": None,
            "source_event_high_watermark_id": target_watermark,
            "projection_rows": len(rows),
            "valid": True,
            "baseline": True,
        }

    baseline_generation_id = _latest_projection_manifest(
        db,
        cutoff=generation.cutoff,
        current_generation_id=int(generation.id),
    )
    if baseline_generation_id is None:
        if target_watermark == 0 and not rows:
            raise MaterialCustodySnapshotUnavailable(
                expected_generation_id=int(generation.id),
                stored_generation_id=None,
                reason=(
                    "custody snapshot baseline is missing; no completed manifest exists "
                    "for earlier Ledger generation"
                ),
            )
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=None,
            reason="custody snapshot baseline is missing; no completed manifest exists for earlier Ledger generation",
        )

    baseline_manifest = _read_manifest(db, generation_id=baseline_generation_id)
    if baseline_manifest is None:
        raise MaterialCustodySnapshotUnavailable(
            manifest_generation_id=baseline_generation_id,
            expected_generation_id=int(generation.id),
            stored_generation_id=None,
            reason="custody snapshot baseline manifest record is missing",
        )
    baseline_generation = db.get(models.LedgerGeneration, baseline_generation_id)
    if baseline_generation is None or baseline_generation.cutoff is None:
        raise MaterialCustodySnapshotUnavailable(
            manifest_generation_id=baseline_generation_id,
            expected_generation_id=int(generation.id),
            stored_generation_id=baseline_generation_id,
            reason="custody snapshot baseline Ledger generation is missing",
        )

    baseline_high_watermark_id = int(baseline_manifest.source_event_high_watermark_id)
    _, expected_rows = _build_projection_from_seed_and_events(
        db,
        generation=generation,
        baseline_generation_id=baseline_generation_id,
        baseline_high_watermark_id=baseline_high_watermark_id,
        baseline_cutoff=baseline_generation.cutoff,
        target_high_watermark_id=target_watermark,
    )

    observed_rows = _projection_rows_by_key(rows)
    if observed_rows != expected_rows:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=int(generation.id),
            reason="custody snapshot rows are not a valid fold from baseline",
        )

    return {
        "ledger_generation_id": int(generation.id),
        "baseline_generation_id": int(baseline_generation_id),
        "source_event_high_watermark_id": target_watermark,
        "projection_rows": len(observed_rows),
        "valid": True,
    }



def load_material_custody_projection(
    db: Session,
    *,
    ledger_generation_id: int,
) -> MaterialCustodyState:
    """
    Fold custody state from a generation-bound manifest + persisted projection/cell rows.

    If no persisted projection exists for this generation, the fold starts from the
    latest earlier completed manifest and replays explicit event deltas between
    baseline and manifest watermarks.
    """
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None or generation.cutoff is None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(ledger_generation_id),
            stored_generation_id=None,
            reason="target Ledger generation is missing or has no cutoff",
        )

    manifest = _read_manifest(db, generation_id=int(generation.id))
    if manifest is None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=None,
            reason="custody snapshot manifest is missing",
        )

    if str(manifest.status) != "complete":
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=int(generation.id),
            stored_generation_id=int(manifest.ledger_generation_id),
            reason="custody snapshot manifest is not complete",
        )
    _require_manifest_cutoff(manifest, generation)

    observed_watermark = _event_high_watermark_id_at_cutoff(
        db,
        cutoff=generation.cutoff,
    )
    target_watermark = int(manifest.source_event_high_watermark_id)
    stale_cache = target_watermark != observed_watermark
    if stale_cache and bool(manifest.is_baseline):
        # The cutover baseline is stated, not derived: nothing older exists to
        # fold it from, so a movement dated inside it is a real break.
        raise MaterialCustodySnapshotUnavailable(
            manifest_generation_id=int(manifest.ledger_generation_id),
            expected_generation_id=int(generation.id),
            stored_generation_id=int(manifest.ledger_generation_id),
            reason="custody snapshot manifest watermark mismatches visible custody events",
        )

    existing_rows = (
        db.query(models.ProductionMaterialCustodyProjection)
        .filter_by(ledger_generation_id=int(generation.id))
        .all()
    )
    if bool(manifest.is_baseline):
        return _state_from_projection_rows(existing_rows)
    if stale_cache:
        # Events dated inside this generation's window were appended after its
        # projection was built — a recorder re-pull projecting an older posting,
        # a backdated transfer, a repair.  The stored rows are a cache keyed by
        # the watermark they were built at, so a moved watermark means "fold
        # again", not "refuse to answer": every consumer of material coverage
        # went dark on this, which is a far worse answer than a slightly more
        # expensive read.
        target_watermark = observed_watermark
        existing_rows = []
    if existing_rows:
        distinct_counts = {
            int(row.source_event_high_watermark_id)
            for row in existing_rows
            if row.source_event_high_watermark_id is not None
        }
        if len(distinct_counts) > 1:
            raise MaterialCustodySnapshotUnavailable(
                expected_generation_id=int(generation.id),
                stored_generation_id=int(generation.id),
                reason="custody snapshot row provenance is malformed",
            )
        if distinct_counts and next(iter(distinct_counts)) != target_watermark:
            raise MaterialCustodySnapshotUnavailable(
                expected_generation_id=int(generation.id),
                stored_generation_id=int(generation.id),
                reason="custody snapshot row watermark mismatches generation manifest",
            )
        return _state_from_projection_rows(existing_rows)

    baseline_generation_id, baseline_manifest, baseline_generation = (
        _resolve_projection_baseline(
            db,
            generation=generation,
            target_high_watermark_id=target_watermark,
        )
    )
    baseline_high_watermark_id = int(baseline_manifest.source_event_high_watermark_id)

    state, _ = _build_projection_from_seed_and_events(
        db,
        generation=generation,
        baseline_generation_id=baseline_generation_id,
        baseline_high_watermark_id=baseline_high_watermark_id,
        baseline_cutoff=baseline_generation.cutoff,
        target_high_watermark_id=target_watermark,
    )
    return state


def load_current_accepted_material_custody(
    db: Session,
    *,
    consumer: str,
) -> tuple[int, MaterialCustodyState]:
    """Load accepted custody with the live PRODPLAN-owned event tail.

    The accepted generation is an immutable physical snapshot.  Creating a
    material issue, however, appends an operational custody event immediately
    after that snapshot's cutoff.  Requiring a new physical generation for
    every such append makes two consecutive launches impossible: the first
    launch makes the accepted projection stale before the second one starts.

    Keep the accepted projection as the base and fold only local, non-physical
    events appended after its cutoff.  SLE-backed events remain invisible until
    a later physical generation accepts their source movements.
    """
    from .planning_truth import require_accepted_truth

    truth = require_accepted_truth(db, consumer)
    generation_id = int(truth.generation_id)
    manifest = _read_manifest(db, generation_id=generation_id)
    if manifest is None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=generation_id,
            stored_generation_id=None,
            reason="custody snapshot manifest is missing",
        )
    current_event_watermark = int(
        db.query(func.coalesce(func.max(models.ProductionMaterialCustodyEvent.id), 0)).scalar()
        or 0
    )
    manifest_watermark = int(manifest.source_event_high_watermark_id)
    if current_event_watermark < manifest_watermark:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=generation_id,
            stored_generation_id=generation_id,
            reason="accepted custody snapshot watermark is ahead of the event stream",
        )
    state = load_material_custody_projection(
        db,
        ledger_generation_id=generation_id,
    )
    if current_event_watermark == manifest_watermark:
        return generation_id, state

    generation = db.get(models.LedgerGeneration, generation_id)
    if generation is None or generation.cutoff is None:
        raise MaterialCustodySnapshotUnavailable(
            expected_generation_id=generation_id,
            stored_generation_id=generation_id,
            reason="accepted custody Ledger generation has no cutoff",
        )

    # The tail is defined by date, not by append order.  Everything dated at or
    # before the cutoff belongs to the accepted projection, which refolds itself
    # when such an event arrives late — a document projected days after it was
    # posted is ordinary traffic.  Selecting by id instead made every one of
    # those events an error and blocked the launch of production orders.
    tail = (
        db.query(models.ProductionMaterialCustodyEvent)
        .filter(
            models.ProductionMaterialCustodyEvent.effective_at > generation.cutoff
        )
        .order_by(models.ProductionMaterialCustodyEvent.id.asc())
        .all()
    )
    for event in tail:
        # A physical event is accepted only together with the physical Ledger
        # generation that proves its source SLE.  Until then the preceding
        # local transit/workshop reservation remains the safe current state.
        if event.source_sle_id is not None:
            continue
        if str(event.source_kind) not in {"issue_created", "terminal_release"}:
            raise MaterialCustodySnapshotUnavailable(
                product_id=int(event.product_id),
                component_item_id=int(event.component_item_id),
                manifest_generation_id=generation_id,
                expected_generation_id=generation_id,
                stored_generation_id=generation_id,
                reason="unsupported live custody event without a physical source",
            )

        product = _ensure_material_custody_product_state(state, int(event.product_id))
        component_id = int(event.component_item_id)
        warehouse = str(event.warehouse_ref1c or "")
        location = str(event.location_kind or "")
        delta = _to_float(event.delta_qty)
        if location == _LOCATION_TRANSIT:
            bucket = product.in_transit
        elif location == _LOCATION_WORKSHOP:
            bucket = product.at_workshop
        else:
            raise MaterialCustodySnapshotUnavailable(
                product_id=int(event.product_id),
                component_item_id=component_id,
                manifest_generation_id=generation_id,
                expected_generation_id=generation_id,
                stored_generation_id=generation_id,
                reason="unsupported live custody event location",
            )

        next_product_qty = bucket.get(component_id, 0.0) + delta
        next_warehouse_qty = state.by_warehouse_item.get((warehouse, component_id), 0.0) + delta
        if next_product_qty < -_EPSILON or next_warehouse_qty < -_EPSILON:
            raise MaterialCustodySnapshotUnavailable(
                product_id=int(event.product_id),
                component_item_id=component_id,
                manifest_generation_id=generation_id,
                expected_generation_id=generation_id,
                stored_generation_id=generation_id,
                reason=f"live custody event produced negative {location} reservation",
            )
        if next_product_qty <= _EPSILON:
            bucket.pop(component_id, None)
        else:
            bucket[component_id] = next_product_qty
        if next_warehouse_qty <= _EPSILON:
            state.by_warehouse_item.pop((warehouse, component_id), None)
        else:
            state.by_warehouse_item[(warehouse, component_id)] = next_warehouse_qty

    return generation_id, state
