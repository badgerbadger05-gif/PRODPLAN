"""Persisted read boundary for the DBR feeder cockpit.

The four GETs mounted by ``DbrFeederPage`` must read one coherent payload.
Live calculators are invoked only by :func:`build_cockpit_snapshot`, which is
intended for a worker or an explicit command.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Optional

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from ... import models
from ..planning_truth import (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_DBR_FEEDER_COCKPIT,
    PlanningTruthUnavailable,
    get_latest_read_snapshot,
    get_truth_state,
    require_accepted_truth,
)


CONSUMER = "dbr_feeder_cockpit"
REQUIRED_CAPABILITIES = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_DBR_FEEDER_COCKPIT,
)
_PAYLOAD_KEYS = ("positions", "signals", "deficits", "processing_board")


class DbrCockpitSnapshotUnavailable(RuntimeError):
    """The current accepted Ledger has no usable cockpit snapshot."""

    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail.get("reason") or "DBR cockpit snapshot unavailable"))

    def as_dict(self) -> dict[str, Any]:
        return dict(self.detail)


def _unavailable(
    db: Session,
    *,
    reason: str,
    truth_detail: Optional[dict[str, Any]] = None,
) -> DbrCockpitSnapshotUnavailable:
    state = get_truth_state(db)
    detail = {
        "code": "dbr_cockpit_snapshot_unavailable",
        "consumer": CONSUMER,
        "status": "unavailable",
        "truth_status": state.status,
        "ledger_generation": state.generation_id,
        "cutoff": state.cutoff.isoformat() if state.cutoff else None,
        "reason": reason,
    }
    if truth_detail:
        detail["truth"] = jsonable_encoder(truth_detail)
    return DbrCockpitSnapshotUnavailable(detail)


def read_cockpit_snapshot(db: Session) -> dict[str, Any]:
    """Return the latest payload for exactly the currently accepted Ledger."""
    try:
        snapshot = get_latest_read_snapshot(
            db,
            consumer=CONSUMER,
            required_capabilities=REQUIRED_CAPABILITIES,
        )
    except PlanningTruthUnavailable as exc:
        raise _unavailable(
            db,
            reason=str(exc),
            truth_detail=exc.as_dict(),
        ) from exc
    if snapshot is None:
        raise _unavailable(
            db,
            reason="No DBR feeder cockpit snapshot for current accepted Ledger",
        )
    payload = snapshot.payload
    if not isinstance(payload, dict) or any(key not in payload for key in _PAYLOAD_KEYS):
        raise _unavailable(
            db,
            reason=f"DBR feeder cockpit snapshot {snapshot.id} has invalid payload",
        )
    result = dict(payload)
    meta = dict(result.get("meta") or {})
    meta.update(
        {
            "snapshot_id": int(snapshot.id),
            "ledger_generation": int(snapshot.ledger_generation_id),
            "cutoff": snapshot.cutoff.isoformat(),
            "truth_status": str(snapshot.truth_status),
            "truth_reason": snapshot.reason,
        }
    )
    result["meta"] = meta
    return result


def _matches(value: Any, expected: Optional[str]) -> bool:
    return expected is None or str(value or "").strip().casefold() == expected.strip().casefold()


def _position_snapshot_id(row: dict[str, Any]) -> int:
    """Return the immutable identifier of one captured position axis.

    Older snapshots carry the legacy position ``id``.  Ledger-native cockpit
    candidates do not persist a mutable ``DbrSupermarketPosition`` row, so
    their stable identity is the captured item/warehouse/pool axis.
    """
    explicit = row.get("id")
    if explicit is not None:
        try:
            value = int(explicit)
        except (TypeError, ValueError) as exc:
            raise ValueError("snapshot position id is invalid") from exc
        if value <= 0:
            raise ValueError("snapshot position id is invalid")
        return value
    parts = (
        str(row.get("item_id") or "").strip(),
        str(row.get("warehouse_ref1c") or "").strip(),
        str(row.get("planning_stock_pool") or "").strip(),
    )
    if not all(parts[:2]):
        raise ValueError("snapshot position has no stable item/warehouse identity")
    digest = sha256("\x1f".join(parts).encode("utf-8")).digest()
    # Keep the route identifier exactly representable by JavaScript Number.
    return int.from_bytes(digest[:8], "big") & ((1 << 53) - 1) or 1


def _snapshot_positions(db: Session, cockpit: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for captured in cockpit["positions"]:
        row = dict(captured)
        try:
            identifier = _position_snapshot_id(row)
        except ValueError as exc:
            raise _unavailable(db, reason=f"Invalid DBR cockpit position identity: {exc}") from exc
        if identifier in seen:
            raise _unavailable(db, reason="DBR cockpit positions have duplicate stable identifiers")
        seen.add(identifier)
        row["id"] = identifier
        rows.append(row)
    return rows


def get_position_snapshot(db: Session, position_id: int) -> Optional[dict[str, Any]]:
    """Read one position only from the current accepted cockpit snapshot."""
    cockpit = read_cockpit_snapshot(db)
    for row in _snapshot_positions(db, cockpit):
        if int(row["id"]) == int(position_id):
            result = dict(row)
            result["snapshot_meta"] = dict(cockpit["meta"])
            return result
    return None


def _signal_snapshot_id(row: dict[str, Any]) -> int:
    explicit = row.get("id")
    if explicit is not None:
        try:
            value = int(explicit)
        except (TypeError, ValueError) as exc:
            raise ValueError("snapshot signal id is invalid") from exc
        if value > 0:
            return value
    identity = str(row.get("dedup_key") or "").strip()
    if not identity:
        identity = "\x1f".join((
            str(row.get("signal_type") or "").strip(),
            str(row.get("item_id") or "").strip(),
            str(row.get("warehouse_ref1c") or "").strip(),
        ))
    if not identity.strip("\x1f"):
        raise ValueError("snapshot signal has no stable identity")
    digest = sha256(identity.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 53) - 1) or 1


def _snapshot_signals(db: Session, cockpit: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for captured in cockpit["signals"]:
        row = dict(captured)
        try:
            identifier = _signal_snapshot_id(row)
        except ValueError as exc:
            raise _unavailable(db, reason=f"Invalid DBR cockpit signal identity: {exc}") from exc
        if identifier in seen:
            raise _unavailable(db, reason="DBR cockpit signals have duplicate stable identifiers")
        seen.add(identifier)
        row["id"] = identifier
        rows.append(row)
    return rows


def get_signal_snapshot(db: Session, signal_id: int) -> Optional[dict[str, Any]]:
    cockpit = read_cockpit_snapshot(db)
    for row in _snapshot_signals(db, cockpit):
        if int(row["id"]) == int(signal_id):
            result = dict(row)
            result["snapshot_meta"] = dict(cockpit["meta"])
            return result
    return None


def query_positions(
    db: Session,
    *,
    include_live_nfp: bool = False,
    active: Optional[bool] = None,
    active_only: bool = False,
    mode: Optional[str] = None,
    supply: Optional[str] = None,
    warehouse: Optional[str] = None,
    zone: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    rows = _snapshot_positions(db, read_cockpit_snapshot(db))
    effective_active = True if active_only and active is None else active
    needle = (search or "").strip().casefold()
    filtered = [
        row
        for row in rows
        if (effective_active is None or bool(row.get("is_active")) is effective_active)
        and _matches(row.get("mode"), mode)
        and _matches(row.get("supply_type"), supply)
        and _matches(row.get("warehouse_ref1c"), warehouse)
        and _matches((row.get("live_nfp") or {}).get("zone"), zone)
        and (
            not needle
            or needle in str(row.get("item_code") or "").casefold()
            or needle in str(row.get("item_name") or "").casefold()
        )
    ]
    page = filtered[offset : offset + limit]
    if include_live_nfp:
        return page
    return [
        {key: value for key, value in row.items() if key != "live_nfp"}
        for row in page
    ]


def query_signals(
    db: Session,
    *,
    status: Optional[str] = None,
    zone: Optional[str] = None,
    signal_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    cockpit = read_cockpit_snapshot(db)
    rows = _snapshot_signals(db, cockpit)
    needle = (search or "").strip().casefold()
    filtered = [
        row
        for row in rows
        if _matches(row.get("status"), status)
        and _matches(row.get("zone"), zone)
        and _matches(row.get("signal_type"), signal_type)
        and (
            not needle
            or needle in str(row.get("item_code") or "").casefold()
            or needle in str(row.get("item_name") or "").casefold()
        )
    ]
    return filtered[offset : offset + limit]


def get_deficits(db: Session) -> dict[str, Any]:
    return dict(read_cockpit_snapshot(db)["deficits"])


def get_processing_board(db: Session) -> dict[str, Any]:
    return dict(read_cockpit_snapshot(db)["processing_board"])


def build_cockpit_snapshot(db: Session) -> models.PlanningReadSnapshot:
    """Fail closed until the cockpit has a fully Ledger-native builder.

    Calling the old DBR calculators here would stamp mutable
    ``ItemWarehouseStock`` / ``produced_qty`` / live order mirrors as accepted
    Ledger truth.  A future worker may implement this function from the
    accepted generation's StockBin, ReservationEntry and ReservationCoverage,
    but containment is safer than publishing a mixed-source snapshot.
    """
    truth = require_accepted_truth(
        db,
        consumer=CONSUMER,
        required_capabilities=REQUIRED_CAPABILITIES,
    )
    raise _unavailable(
        db,
        reason=(
            "Ledger-native DBR cockpit builder is not implemented; "
            "legacy live calculators are forbidden"
        ),
        truth_detail=truth.as_dict(),
    )
