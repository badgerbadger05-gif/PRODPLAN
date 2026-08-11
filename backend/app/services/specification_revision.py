"""Immutable specification revision snapshots and automatic rebase enqueue."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from app import models


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        return format(value.normalize(), "f")
    return value


def specification_revision_payload(
    db: Session, spec_id: int
) -> dict[str, Any]:
    spec = db.get(models.Specification, int(spec_id))
    if spec is None:
        raise ValueError(f"specification {int(spec_id)} not found")
    components = (
        db.query(models.SpecComponent)
        .filter(models.SpecComponent.spec_id == int(spec.spec_id))
        .order_by(
            models.SpecComponent.item_id,
            models.SpecComponent.component_spec_ref1c,
            models.SpecComponent.component_id,
        )
        .all()
    )
    operations = (
        db.query(models.SpecOperation)
        .filter(models.SpecOperation.spec_id == int(spec.spec_id))
        .order_by(models.SpecOperation.operation_id, models.SpecOperation.spec_operation_id)
        .all()
    )
    return {
        "version": 1,
        "spec_ref1c": str(spec.spec_ref1c or "").strip(),
        "production_kind_id": (
            int(spec.production_kind_id) if spec.production_kind_id is not None else None
        ),
        "components": [
            {
                "item_id": int(row.item_id),
                "quantity": _json_value(row.quantity),
                "stage_id": int(row.stage_id) if row.stage_id is not None else None,
                "component_type": str(row.component_type or ""),
                "component_spec_ref1c": (
                    str(row.component_spec_ref1c).strip()
                    if row.component_spec_ref1c
                    else None
                ),
            }
            for row in components
        ],
        "operations": [
            {
                "operation_id": int(row.operation_id),
                "stage_id": int(row.stage_id) if row.stage_id is not None else None,
                "time_norm": _json_value(row.time_norm),
            }
            for row in operations
        ],
    }


def specification_revision_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def record_specification_revisions(
    db: Session,
    spec_ids: Iterable[int],
    *,
    previous_hash_by_id: Mapping[int, str | None],
    source: str = "odata",
) -> dict[str, Any]:
    """Record exact revisions and enqueue only known old->new transitions."""
    created = 0
    queued = 0
    changed_refs: list[str] = []
    for spec_id in sorted({int(value) for value in spec_ids}):
        spec = db.get(models.Specification, spec_id)
        if spec is None:
            continue
        payload = specification_revision_payload(db, spec_id)
        content_hash = specification_revision_hash(payload)
        previous_hash = previous_hash_by_id.get(spec_id)
        revision = (
            db.query(models.SpecificationRevision)
            .filter(
                models.SpecificationRevision.spec_id == spec_id,
                models.SpecificationRevision.content_hash == content_hash,
            )
            .one_or_none()
        )
        if revision is None:
            revision = models.SpecificationRevision(
                spec_id=spec_id,
                content_hash=content_hash,
                payload=payload,
                source=str(source or "odata"),
            )
            db.add(revision)
            db.flush()
            created += 1
        spec.content_hash = content_hash
        if previous_hash and previous_hash != content_hash:
            request = (
                db.query(models.SpecificationRebaseQueue)
                .filter(models.SpecificationRebaseQueue.revision_id == int(revision.id))
                .one_or_none()
            )
            if request is None:
                db.add(
                    models.SpecificationRebaseQueue(
                        spec_id=spec_id,
                        revision_id=int(revision.id),
                        old_content_hash=str(previous_hash),
                        new_content_hash=content_hash,
                        status="pending",
                    )
                )
                queued += 1
            changed_refs.append(str(spec.spec_ref1c or ""))
    db.flush()
    return {
        "revisions_created": created,
        "rebase_requests_queued": queued,
        "changed_spec_refs": sorted({value for value in changed_refs if value}),
    }
