"""Stable current reservation owner and bounded BUILDING publisher.

``ReservationEntry`` remains the compatibility model used by the planner, but
only rows marked ``is_current`` are accepted truth.  BUILDING rows are a
bounded replay workspace and are collapsed into the stable requirement owner
before the accepted pointer is published.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from .reservation import reservation_business_identity


class ReservationCurrentError(ValueError):
    """Current reservation publication is unavailable and must fail closed."""


def _dialect(db: Session) -> str:
    return str(getattr(getattr(db, "bind", None), "dialect", None).name)


def publish_current_reservations(
    db: Session,
    *,
    generation_id: int,
) -> dict[str, int]:
    """Atomically publish target BUILDING reservations into stable owners.

    PostgreSQL uses set-based temporary mapping and updates.  SQLite keeps a
    bounded compatibility path for unit tests; it is not used for production
    data migration.  The caller owns the transaction and publishes the truth
    pointer only after this function returns.
    """
    generation_id = int(generation_id)
    generation = db.get(models.LedgerGeneration, generation_id)
    if generation is None or str(generation.status) != "building":
        raise ReservationCurrentError("reservation current publication requires BUILDING generation")

    if _dialect(db) != "postgresql":
        return _publish_sqlite_bounded(db, generation_id)

    occupied_owner = db.execute(text("""
        SELECT 1
          FROM reservation_entry
         WHERE ledger_generation_id = :generation_id
           AND owner_kind <> 'building'
         LIMIT 1
    """), {"generation_id": generation_id}).scalar()
    if occupied_owner is not None:
        raise ReservationCurrentError(
            "BUILDING reservation publication contains a non-building owner"
        )
    collision = db.execute(text("""
        SELECT current_identity, count(*)
          FROM reservation_entry
         WHERE ledger_generation_id = :generation_id
         GROUP BY current_identity
        HAVING trim(coalesce(current_identity, '')) = '' OR count(*) > 1
         LIMIT 1
    """), {"generation_id": generation_id}).first()
    if collision is not None:
        raise ReservationCurrentError(
            f"BUILDING reservation identity collision: {collision[0]!r}"
        )

    db.execute(text("""
        CREATE TEMP TABLE reservation_publish_map ON COMMIT DROP AS
        SELECT stage.id AS stage_id,
               coalesce(current_owner.id, stage.id) AS owner_id,
               stage.current_identity
          FROM reservation_entry AS stage
          LEFT JOIN reservation_entry AS current_owner
            ON current_owner.current_identity = stage.current_identity
           AND current_owner.is_current = true
         WHERE stage.ledger_generation_id = :generation_id
           AND stage.owner_kind = 'building'
    """), {"generation_id": generation_id})
    db.execute(text("""
        CREATE TEMP TABLE reservation_publish_before ON COMMIT DROP AS
        SELECT owner.id, owner.current_identity, owner.reserved_qty,
               owner.covered_from_stock_at_freeze_qty,
               owner.replenishment_required_qty, owner.lifecycle_status
          FROM reservation_entry AS owner
          JOIN reservation_publish_map AS map ON map.owner_id = owner.id
         WHERE owner.id <> map.stage_id
    """))
    db.execute(text("""
        CREATE TEMP TABLE reservation_publish_stage ON COMMIT DROP AS
        SELECT stage.*, map.owner_id
          FROM reservation_entry AS stage
          JOIN reservation_publish_map AS map ON map.stage_id = stage.id
    """))
    db.execute(text("""
        CREATE TEMP TABLE reservation_publish_stage_event ON COMMIT DROP AS
        SELECT event.id
          FROM reservation_event AS event
          JOIN reservation_publish_map AS map
            ON map.stage_id = event.reservation_id
    """))
    db.execute(text("""
        CREATE TEMP TABLE reservation_publish_removed ON COMMIT DROP AS
        SELECT owner.id, owner.current_identity, owner.reserved_qty,
               owner.covered_from_stock_at_freeze_qty,
               owner.replenishment_required_qty, owner.lifecycle_status
          FROM reservation_entry AS owner
         WHERE owner.is_current = true
           AND NOT EXISTS (
               SELECT 1 FROM reservation_publish_map AS map
                WHERE map.owner_id = owner.id
           )
    """))

    # Classify target events while they are still BUILDING.  This does not
    # touch is_current, so the partial current-identity index cannot collide.
    db.execute(text("""
        UPDATE reservation_event AS event
           SET origin_kind = CASE
               WHEN event.sle_id IS NULL AND event.realized_delta = 0 THEN 'obligation'
               WHEN event.event_kind = 'unrealize' OR event.realized_delta < 0 THEN 'correction'
               ELSE 'factual'
           END
         WHERE event.reservation_id IN (
             SELECT stage_id FROM reservation_publish_map
         )
    """))

    # Every staging reservation must have exactly one obligation basis.  LEFT
    # JOIN is intentional: it rejects both zero and duplicate basis rows.
    obligation_conflict = db.execute(text("""
        SELECT stage.id, count(event.id)
          FROM reservation_entry AS stage
          LEFT JOIN reservation_event AS event
            ON event.reservation_id = stage.id
           AND event.origin_kind = 'obligation'
         WHERE stage.ledger_generation_id = :generation_id
           AND stage.owner_kind = 'building'
         GROUP BY stage.id
        HAVING count(event.id) <> 1
         LIMIT 1
    """), {"generation_id": generation_id}).first()
    if obligation_conflict is not None:
        raise ReservationCurrentError(
            "reservation publication requires exactly one obligation basis for "
            f"staging reservation {obligation_conflict[0]} (found {obligation_conflict[1]})"
        )

    # A changed obligation is a replacement of the current basis, not an
    # additional reserve.  Retain the old row as historical non-current audit
    # and let the target BUILDING basis become the sole current one.
    db.execute(text("""
        UPDATE reservation_event AS old_event
           SET is_current = false
          FROM reservation_publish_map AS map
         WHERE old_event.reservation_id = map.owner_id
           AND old_event.is_current = true
           AND old_event.origin_kind = 'obligation'
           AND EXISTS (
               SELECT 1
                 FROM reservation_event AS stage_event
                WHERE stage_event.reservation_id = map.stage_id
                  AND stage_event.origin_kind = 'obligation'
           )
    """))
    event_conflict = db.execute(text("""
        SELECT event.idempotency_key, count(DISTINCT event.event_identity)
          FROM reservation_event AS event
         WHERE event.reservation_id IN (
             SELECT stage_id FROM reservation_publish_map
             UNION SELECT owner_id FROM reservation_publish_map
         )
         GROUP BY event.idempotency_key
        HAVING count(DISTINCT event.event_identity) > 1
         LIMIT 1
    """)).first()
    if event_conflict is not None:
        raise ReservationCurrentError(
            f"reservation event idempotency collision: {event_conflict[0]!r}"
        )
    # Remove semantic replay copies before making target events current.  A
    # current factual event wins; an old obligation was demoted above, so the
    # target obligation basis wins even when its identity is unchanged.
    db.execute(text("""
        DELETE FROM reservation_event AS stage_event
         USING reservation_publish_map AS map,
               reservation_event AS current_event
         WHERE stage_event.reservation_id = map.stage_id
           AND current_event.reservation_id = map.owner_id
           AND current_event.is_current = true
           AND current_event.event_identity = stage_event.event_identity
           AND stage_event.event_identity <> ''
           AND stage_event.origin_kind <> 'obligation'
    """))
    db.execute(text("""
        DELETE FROM reservation_event AS event
         USING (
               SELECT event.id,
                      row_number() OVER (
                          PARTITION BY event.event_identity
                          ORDER BY event.id ASC
                      ) AS ordinal
                 FROM reservation_event AS event
                 JOIN reservation_publish_map AS map
                   ON map.stage_id = event.reservation_id
                WHERE event.event_identity <> ''
              ) AS ranked
         WHERE ranked.id = event.id AND ranked.ordinal > 1
    """))
    # Rebind all durable references before removing staging rows.
    for table in (
        "reservation_consumption_allocation",
        "current_replenishment_audit",
        "replenishment_work_item",
        "purchase_export_obligation_allocation",
    ):
        db.execute(text(f"""
            UPDATE {table} AS target
               SET reservation_id = map.owner_id
              FROM reservation_publish_map AS map
             WHERE target.reservation_id = map.stage_id
               AND map.stage_id <> map.owner_id
        """))

    # Only now can a BUILDING event take the current partial-index slot.
    db.execute(text("""
        UPDATE reservation_event AS event
           SET reservation_id = map.owner_id,
               is_current = true
          FROM reservation_publish_map AS map
         WHERE event.reservation_id = map.stage_id
    """))
    db.execute(text("""
        DELETE FROM reservation_event AS event
         USING (
               SELECT event.id,
                      row_number() OVER (
                          PARTITION BY event.event_identity
                          ORDER BY event.is_current DESC, event.id ASC
                      ) AS ordinal
                 FROM reservation_event AS event
                WHERE event.reservation_id IN (
                    SELECT stage_id FROM reservation_publish_map
                    UNION SELECT owner_id FROM reservation_publish_map
                )
                  AND event.event_identity <> ''
              ) AS ranked
         WHERE ranked.id = event.id AND ranked.ordinal > 1
    """))
    db.execute(text("""
        DELETE FROM reservation_event AS event
         USING (
               SELECT event.id,
                      row_number() OVER (
                          PARTITION BY event.idempotency_key
                          ORDER BY event.is_current DESC, event.id ASC
                      ) AS ordinal
                 FROM reservation_event AS event
                WHERE event.reservation_id IN (
                    SELECT stage_id FROM reservation_publish_map
                    UNION SELECT owner_id FROM reservation_publish_map
                )
              ) AS ranked
         WHERE ranked.id = event.id AND ranked.ordinal > 1
    """))
    # Exact replay retry is not a semantic change.  Keep one event per stable
    # identity; staging rows become current only at this publication boundary.
    db.execute(text("""
        DELETE FROM reservation_event AS event
         USING (
               SELECT id,
                      row_number() OVER (
                          PARTITION BY event_identity
                          ORDER BY id ASC
                      ) AS ordinal
                 FROM reservation_event
                WHERE event_identity <> ''
                  AND is_current = true
              ) AS ranked
         WHERE ranked.id = event.id AND ranked.ordinal > 1
    """))
    obligation_basis_conflict = db.execute(text("""
        SELECT map.owner_id, count(event.id)
          FROM reservation_publish_map AS map
          LEFT JOIN reservation_event AS event
            ON event.reservation_id = map.owner_id
           AND event.is_current = true
           AND event.origin_kind = 'obligation'
         GROUP BY map.owner_id
         HAVING count(event.id) <> 1
         LIMIT 1
    """)).first()
    if obligation_basis_conflict is not None:
        raise ReservationCurrentError(
            "reservation publication did not produce one current obligation basis "
            f"for owner {obligation_basis_conflict[0]}"
        )
    # Generation provenance is rebound only after semantic and idempotency
    # de-duplication, so the legacy generation/idempotency unique constraint is
    # never violated transiently.
    db.execute(text("""
        UPDATE reservation_event AS event
           SET ledger_generation_id = :generation_id
          FROM reservation_publish_stage_event AS stage_event
         WHERE stage_event.id = event.id
    """), {"generation_id": generation_id})

    db.execute(text("""
        DELETE FROM reservation_entry AS stage
         USING reservation_publish_map AS map
         WHERE stage.id = map.stage_id AND map.stage_id <> map.owner_id
    """))
    db.execute(text("""
        UPDATE reservation_entry AS owner
           SET item_id = stage.item_id,
               characteristic_ref = stage.characteristic_ref,
               organization_ref = stage.organization_ref,
               planning_stock_pool = stage.planning_stock_pool,
               run_id = stage.run_id,
               freeze_version = stage.freeze_version,
               requirement_id = stage.requirement_id,
               priority_period_from = stage.priority_period_from,
               priority_period_to = stage.priority_period_to,
               realization_mode = stage.realization_mode,
               reserved_qty = stage.reserved_qty,
               covered_from_stock_at_freeze_qty = stage.covered_from_stock_at_freeze_qty,
               replenishment_required_qty = stage.replenishment_required_qty,
               replenishment_received_qty = stage.replenishment_received_qty,
               realized_qty = stage.realized_qty,
               lifecycle_status = stage.lifecycle_status,
               opened_at = stage.opened_at,
               closed_at = stage.closed_at,
               ledger_generation_id = :generation_id,
               owner_kind = 'current',
               is_current = true
          FROM reservation_publish_stage AS stage
         WHERE owner.id = stage.owner_id
    """), {"generation_id": generation_id})
    db.execute(text("""
        UPDATE reservation_entry AS owner
           SET lifecycle_status = 'closed', closed_at = current_timestamp
          FROM reservation_publish_removed AS removed
         WHERE owner.id = removed.id AND owner.lifecycle_status <> 'closed'
    """))
    db.execute(text("""
        INSERT INTO reservation_current_change (
            current_identity, reservation_id, source_generation_id,
            operation, origin_kind, before_payload, after_payload
        )
        SELECT removed.current_identity, removed.id, :generation_id,
               'close', 'obligation',
               json_build_object('reserved_qty', removed.reserved_qty,
                                  'required_qty', removed.replenishment_required_qty,
                                  'lifecycle_status', removed.lifecycle_status),
               json_build_object('lifecycle_status', 'closed')
          FROM reservation_publish_removed AS removed
         WHERE removed.lifecycle_status <> 'closed'
    """), {"generation_id": generation_id})
    db.execute(text("""
        INSERT INTO reservation_current_change (
            current_identity, reservation_id, source_generation_id,
            operation, origin_kind, before_payload, after_payload
        )
        SELECT before.current_identity, before.id, :generation_id,
               CASE WHEN before.lifecycle_status = 'closed'
                    AND after.lifecycle_status = 'active' THEN 'reopen'
                    ELSE 'update' END,
               'obligation',
               json_build_object('reserved_qty', before.reserved_qty,
                                  'covered_qty', before.covered_from_stock_at_freeze_qty,
                                  'required_qty', before.replenishment_required_qty,
                                  'lifecycle_status', before.lifecycle_status),
               json_build_object('reserved_qty', after.reserved_qty,
                                  'covered_qty', after.covered_from_stock_at_freeze_qty,
                                  'required_qty', after.replenishment_required_qty,
                                  'lifecycle_status', after.lifecycle_status)
          FROM reservation_publish_before AS before
          JOIN reservation_entry AS after ON after.id = before.id
         WHERE before.reserved_qty IS DISTINCT FROM after.reserved_qty
            OR before.covered_from_stock_at_freeze_qty IS DISTINCT FROM after.covered_from_stock_at_freeze_qty
            OR before.replenishment_required_qty IS DISTINCT FROM after.replenishment_required_qty
            OR before.lifecycle_status IS DISTINCT FROM after.lifecycle_status
    """), {"generation_id": generation_id})
    db.execute(text("""
        INSERT INTO reservation_current_change (
            current_identity, reservation_id, source_generation_id,
            operation, origin_kind, before_payload, after_payload
        )
        SELECT stage.current_identity, stage.owner_id, :generation_id,
               'insert', 'obligation', NULL,
               json_build_object('reserved_qty', stage.reserved_qty,
                                  'covered_qty', stage.covered_from_stock_at_freeze_qty,
                                  'required_qty', stage.replenishment_required_qty,
                                  'lifecycle_status', stage.lifecycle_status)
          FROM reservation_publish_stage AS stage
         WHERE NOT EXISTS (
             SELECT 1 FROM reservation_publish_before AS before
              WHERE before.id = stage.owner_id
         )
    """), {"generation_id": generation_id})
    db.execute(text("""
        UPDATE reservation_entry
           SET owner_kind = 'current', is_current = true
         WHERE ledger_generation_id = :generation_id
           AND owner_kind = 'building'
    """), {"generation_id": generation_id})
    return {"published": int(db.execute(text(
        "SELECT count(*) FROM reservation_entry WHERE ledger_generation_id = :id AND is_current = true"
    ), {"id": generation_id}).scalar() or 0)}


def _publish_sqlite_bounded(db: Session, generation_id: int) -> dict[str, int]:
    """Small compatibility path for ephemeral tests; never used by prod migration."""
    rows = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == generation_id,
        models.ReservationEntry.owner_kind == "building",
    ).order_by(models.ReservationEntry.id.asc()).all()
    by_identity: dict[str, models.ReservationEntry] = {}
    payload_fields = (
        "item_id", "characteristic_ref", "organization_ref", "planning_stock_pool",
        "run_id", "freeze_version", "requirement_id", "priority_period_from",
        "priority_period_to", "realization_mode", "reserved_qty",
        "covered_from_stock_at_freeze_qty", "replenishment_required_qty",
        "replenishment_received_qty", "realized_qty", "lifecycle_status",
        "opened_at", "closed_at",
    )

    def _payload(row: models.ReservationEntry) -> dict[str, Any]:
        def _json_value(value: Any) -> Any:
            if isinstance(value, (datetime,)):
                return value.isoformat()
            if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
                return value.isoformat()
            if isinstance(value, Decimal):
                return format(value, "f")
            return value

        return {
            name: _json_value(getattr(row, name))
            for name in payload_fields
        }

    def _record_change(
        owner: models.ReservationEntry,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        operation: str,
    ) -> None:
        if before == after:
            return
        db.add(models.ReservationCurrentChange(
            current_identity=str(owner.current_identity or ""),
            reservation_id=int(owner.id),
            source_generation_id=generation_id,
            operation=operation,
            origin_kind="obligation",
            before_payload=before,
            after_payload=after,
        ))

    # Keep the compatibility path fail-closed with the production contract:
    # every BUILDING reservation needs one and only one obligation basis.
    staged_events: dict[int, list[models.ReservationEvent]] = {}
    for stage in rows:
        events = db.query(models.ReservationEvent).filter(
            models.ReservationEvent.reservation_id == stage.id
        ).all()
        for event in events:
            event.origin_kind = (
                "obligation" if event.sle_id is None and event.realized_delta == 0
                else "correction" if event.event_kind == "unrealize" or event.realized_delta < 0
                else "factual"
            )
        obligation_events = [
            event for event in events if event.origin_kind == "obligation"
        ]
        if len(obligation_events) != 1:
            raise ReservationCurrentError(
                "BUILDING reservation requires exactly one obligation basis: "
                f"reservation {stage.id} has {len(obligation_events)}"
            )
        if any(not str(event.event_identity or "") for event in events):
            raise ReservationCurrentError(
                f"BUILDING reservation {stage.id} has an event without stable identity"
            )
        staged_events[int(stage.id)] = events

    for stage in rows:
        identity = str(stage.current_identity or "").strip()
        if not identity:
            identity = reservation_business_identity(stage.requirement_id, stage.realization_mode)
            stage.current_identity = identity
        if identity in by_identity and by_identity[identity].id != stage.id:
            raise ReservationCurrentError(f"BUILDING reservation identity collision: {identity}")
        current = db.query(models.ReservationEntry).filter(
            models.ReservationEntry.current_identity == identity,
            models.ReservationEntry.is_current.is_(True),
        ).one_or_none()
        owner = current or by_identity.get(identity) or stage
        by_identity[identity] = owner
        before = _payload(owner) if owner.id != stage.id else None
        stage_values = {
            name: getattr(stage, name)
            for name in payload_fields
        }
        if owner.id != stage.id:
            for model in (
                models.ReservationConsumptionAllocation,
                models.CurrentReplenishmentAudit,
                models.ReplenishmentWorkItem,
                models.PurchaseExportObligationAllocation,
            ):
                db.query(model).filter(model.reservation_id == stage.id).update(
                    {"reservation_id": owner.id}, synchronize_session=False
                )
            owner_events = db.query(models.ReservationEvent).filter(
                models.ReservationEvent.reservation_id == owner.id
            ).all()
            stage_events = staged_events[int(stage.id)]
            if any(str(event.origin_kind) == "obligation" for event in stage_events):
                for event in owner_events:
                    if event.is_current and str(event.origin_kind) == "obligation":
                        event.is_current = False
            event_by_identity = {
                str(event.event_identity): event
                for event in owner_events
                if str(event.event_identity or "")
            }
            idempotency_by_key = {
                str(event.idempotency_key): str(event.event_identity or "")
                for event in owner_events
            }
            for event in stage_events:
                if not str(event.event_identity or ""):
                    raise ReservationCurrentError("BUILDING reservation event has no stable identity")
                prior_identity = idempotency_by_key.get(str(event.idempotency_key))
                if prior_identity is not None and prior_identity != str(event.event_identity):
                    raise ReservationCurrentError(
                        f"reservation event idempotency collision: {event.idempotency_key!r}"
                    )
                existing = event_by_identity.get(str(event.event_identity))
                if existing is not None:
                    existing.is_current = True
                    db.delete(event)
                    continue
                event.reservation_id = owner.id
                event.ledger_generation_id = generation_id
                event.origin_kind = (
                    "obligation" if event.sle_id is None and event.realized_delta == 0
                    else "correction" if event.event_kind == "unrealize" or event.realized_delta < 0
                    else "factual"
                )
                event.is_current = True
                event_by_identity[str(event.event_identity)] = event
                idempotency_by_key[str(event.idempotency_key)] = str(event.event_identity)
            db.delete(stage)
            # SQLite enforces the legacy (generation, requirement) unique key
            # immediately; remove the staging row before moving the owner to
            # the target generation.
            db.flush()
            for name, value in stage_values.items():
                setattr(owner, name, value)
            after = _payload(owner)
            _record_change(
                owner,
                before,
                after,
                "reopen" if before and before.get("lifecycle_status") == "closed"
                and after.get("lifecycle_status") == "active" else "update",
            )
        else:
            for event in staged_events[int(stage.id)]:
                event.origin_kind = (
                    "obligation" if event.sle_id is None and event.realized_delta == 0
                    else "correction" if event.event_kind == "unrealize" or event.realized_delta < 0
                    else "factual"
                )
                event.is_current = True
                event.ledger_generation_id = generation_id
        owner.owner_kind = "current"
        owner.is_current = True
        owner.ledger_generation_id = generation_id
        if before is None:
            _record_change(owner, None, _payload(owner), "insert")

    staged_identities = set(by_identity)
    for owner in db.query(models.ReservationEntry).filter(
        models.ReservationEntry.is_current.is_(True),
    ).all():
        if str(owner.current_identity or "") in staged_identities:
            continue
        if str(owner.lifecycle_status) != "closed":
            before = _payload(owner)
            owner.lifecycle_status = "closed"
            owner.closed_at = datetime.now(timezone.utc)
            owner.is_current = False
            owner.owner_kind = "legacy"
            _record_change(owner, before, _payload(owner), "close")
    db.flush()
    return {"published": len(by_identity)}


def current_reservation_query(
    db: Session, *, generation_id: int, allow_building: bool = False
):
    """Return BUILDING staging or accepted rows for one exact truth pointer."""
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ReservationCurrentError("reservation generation is unavailable")
    query = db.query(models.ReservationEntry)
    if str(generation.status) == "building":
        if not allow_building:
            raise ReservationCurrentError("BUILDING reservation truth is not readable")
        return query.filter(
            models.ReservationEntry.ledger_generation_id == int(generation_id),
            models.ReservationEntry.owner_kind == "building",
        )
    if str(generation.status) != "accepted":
        raise ReservationCurrentError("accepted reservation truth is unavailable")
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or int(pointer.current_generation_id or 0) != int(generation_id):
        raise ReservationCurrentError("reservation generation is not exact planning truth")
    return query.filter(models.ReservationEntry.is_current.is_(True))
