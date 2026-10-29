"""One-time, validated reference import for component feeder assignments."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app import models


SOURCE_NAME = "ProdFlowSupermarketPosition one-time snapshot"
MANIFEST_ID = 1


class ProdFlowFeederUnavailable(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _revision(rows: Iterable[dict[str, Any]]) -> str:
    payload = json.dumps(list(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _active(value: Any) -> bool:
    if value in (True, 1, "1"):
        return True
    if value in (False, 0, "0"):
        return False
    raise ProdFlowFeederUnavailable(f"ProdFlow feeder is_active is invalid ({value!r})")


def publish_prodflow_feeder_assignments(
    db: Session,
    *,
    positions: list[dict[str, Any]],
    warehouses: list[dict[str, Any]],
    observed_at: datetime,
) -> dict[str, Any]:
    """Validate one complete captured snapshot, then atomically replace config."""
    warehouse_refs: dict[str, str] = {}
    for row in warehouses:
        name = str(row.get("name") or "").strip()
        ref = str(row.get("custom_ref1c") or "").strip()
        if not name or not ref:
            continue
        previous = warehouse_refs.get(name)
        if previous is not None and previous != ref:
            raise ProdFlowFeederUnavailable(
                f"ProdFlow warehouse name has conflicting custom_ref1c (warehouse={name})"
            )
        warehouse_refs[name] = ref
    local_warehouse_refs = {
        str(row[0])
        for row in db.query(models.StockWarehouse.warehouse_ref1c).all()
        if str(row[0] or "").strip()
    }
    item_by_code = {
        str(item.item_code or "").strip(): int(item.item_id)
        for item in db.query(models.Item).all()
        if str(item.item_code or "").strip()
    }
    accepted: dict[int, dict[str, Any]] = {}
    for row in positions:
        if not _active(row.get("is_active")):
            continue
        item_code = str(row.get("item") or "").strip()
        item_id = item_by_code.get(item_code)
        if item_id is None:
            raise ProdFlowFeederUnavailable(
                f"ProdFlow feeder item is absent from PRODPLAN Item (item={item_code})"
            )
        warehouse_name = str(row.get("warehouse") or "").strip()
        warehouse_ref = warehouse_refs.get(warehouse_name)
        if not warehouse_ref:
            raise ProdFlowFeederUnavailable(
                f"ProdFlow feeder warehouse has no custom_ref1c (item={item_code}, warehouse={warehouse_name})"
            )
        if warehouse_ref not in local_warehouse_refs:
            raise ProdFlowFeederUnavailable(
                f"ProdFlow feeder warehouse is absent from PRODPLAN StockWarehouse "
                f"(item={item_code}, warehouse_ref1c={warehouse_ref})"
            )
        if item_id in accepted:
            raise ProdFlowFeederUnavailable(
                f"ProdFlow feeder assignment is ambiguous (item={item_code})"
            )
        accepted[item_id] = {
            "item_id": item_id,
            "item": item_code,
            "warehouse_ref1c": warehouse_ref,
            "route_class": str(row.get("route_class") or "").strip() or None,
        }

    canonical = [accepted[item_id] for item_id in sorted(accepted)]
    if not canonical:
        raise ProdFlowFeederUnavailable("ProdFlow feeder snapshot has no active assignments")
    revision = _revision(canonical)
    manifest = db.get(models.ProdFlowFeederManifest, MANIFEST_ID)
    previous_revision = str(manifest.source_revision) if manifest is not None else None
    persisted = []
    persisted_revisions_match = False
    if manifest is not None:
        persisted_rows = (
            db.query(models.ProdFlowFeederAssignment)
            .order_by(models.ProdFlowFeederAssignment.item_id.asc())
            .all()
        )
        persisted = [
            {
                "item_id": int(row.item_id),
                "item": str(row.source_item_code),
                "warehouse_ref1c": str(row.warehouse_ref1c),
                "route_class": str(row.route_class) if row.route_class is not None else None,
            }
            for row in persisted_rows
        ]
        persisted_revisions_match = all(
            str(row.source_revision) == revision for row in persisted_rows
        )
    if (
        manifest is not None
        and str(manifest.status or "") == "complete"
        and previous_revision == revision
        and int(manifest.row_count or 0) == len(canonical)
        and persisted_revisions_match
        and persisted == canonical
    ):
        return {
            "status": "complete",
            "source_revision": revision,
            "rows": len(canonical),
            "warehouses": len({row["warehouse_ref1c"] for row in canonical}),
            "observed_at": _utc(manifest.loaded_at).isoformat(),
            "changed": False,
        }
    db.query(models.ProdFlowFeederAssignment).delete(synchronize_session="fetch")
    for row in canonical:
        db.add(models.ProdFlowFeederAssignment(
            item_id=int(row["item_id"]),
            warehouse_ref1c=str(row["warehouse_ref1c"]),
            source_item_code=str(row["item"]),
            route_class=row["route_class"],
            source_revision=revision,
        ))
    if manifest is None:
        manifest = models.ProdFlowFeederManifest(id=MANIFEST_ID)
        db.add(manifest)
    manifest.status = "complete"
    manifest.source_revision = revision
    manifest.row_count = len(canonical)
    manifest.loaded_at = _utc(observed_at)
    manifest.source_name = SOURCE_NAME
    manifest.last_error = None
    if previous_revision != revision:
        from .item_ledger.current_execution import invalidate_current_execution_scope

        invalidate_current_execution_scope(
            db,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            source_revision=f"prodflow-feeders:{revision}",
            reason="prodflow_feeder_assignment_changed",
        )
    db.flush()
    return {
        "status": "complete",
        "source_revision": revision,
        "rows": len(canonical),
        "warehouses": len({row["warehouse_ref1c"] for row in canonical}),
        "observed_at": _utc(observed_at).isoformat(),
        "changed": True,
    }


def feeder_warehouse_snapshot(
    db: Session,
    item_ids: Iterable[int],
) -> tuple[dict[int, str], str]:
    """Read the sealed static config; missing keys remain explicit to caller."""
    manifest = db.get(models.ProdFlowFeederManifest, MANIFEST_ID)
    if manifest is None or str(manifest.status or "") != "complete":
        raise ProdFlowFeederUnavailable("ProdFlow feeder assignments are not imported")
    requested = sorted({int(value) for value in item_ids})
    rows = db.query(models.ProdFlowFeederAssignment).filter(
        models.ProdFlowFeederAssignment.item_id.in_(requested)
    ).all() if requested else []
    if any(str(row.source_revision) != str(manifest.source_revision) for row in rows):
        raise ProdFlowFeederUnavailable("ProdFlow feeder assignment revision is mixed")
    return (
        {int(row.item_id): str(row.warehouse_ref1c) for row in rows},
        str(manifest.source_revision),
    )


def feeder_warehouse_for_items(db: Session, item_ids: Iterable[int]) -> dict[int, str]:
    requested = sorted({int(value) for value in item_ids})
    result, _revision = feeder_warehouse_snapshot(db, requested)
    missing = [item_id for item_id in requested if item_id not in result]
    if missing:
        raise ProdFlowFeederUnavailable(
            "ProdFlow feeder assignment is missing for component_item_id="
            + ",".join(map(str, missing[:8]))
        )
    return result
