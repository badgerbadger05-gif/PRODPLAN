"""Bounded compact-current StockBin refresh for physical refresh candidates.

The routine consumes an explicit delta manifest. It never folds a historical
prefix: the accepted current StockBin is the opening basis and only rows and
supersession edges in the target import window are queried.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app import models
from .physical import LedgerKey, fold_running_balance
from .physical_visibility import PhysicalVisibilityError, require_import_batch


class BoundedStockBinRefreshError(ValueError):
    """The bounded StockBin refresh input or current owner is unsafe."""


def _comparable_datetime(value: datetime | None) -> datetime | None:
    """Normalize DB-naive and API-aware timestamps for Python comparisons."""
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


@dataclass(frozen=True)
class BoundedPhysicalDeltaManifest:
    """Persisted IDs proving the complete bounded target delta."""

    new_sle_ids: tuple[int, ...] = ()
    supersession_edge_ids: tuple[int, ...] = ()
    backdate_from: datetime | None = None


@dataclass(frozen=True)
class BoundedStockBinRefreshResult:
    parent_generation_id: int
    target_generation_id: int
    affected_keys: tuple[LedgerKey, ...]
    changed_keys: tuple[LedgerKey, ...]
    semantic_noop_keys: tuple[LedgerKey, ...]
    delta_rows: int

    @property
    def changed_rows(self) -> int:
        return len(self.changed_keys)

    @property
    def visible_fact_rows(self) -> int:
        """Compatibility metric name; it counts only manifest delta rows."""
        return self.delta_rows


def _normalise_keys(affected_keys: Iterable[Any]) -> tuple[LedgerKey, ...]:
    if affected_keys is None:
        raise BoundedStockBinRefreshError("affected physical keys are required")
    result: list[LedgerKey] = []
    seen: set[LedgerKey] = set()
    for raw in affected_keys:
        try:
            values = tuple(raw)
        except TypeError as exc:
            raise BoundedStockBinRefreshError("affected physical key is malformed") from exc
        if len(values) != 4:
            raise BoundedStockBinRefreshError(
                "affected physical key must contain item, characteristic, organization and warehouse"
            )
        if values[0] in (None, "") or any(value is None for value in values[1:]):
            raise BoundedStockBinRefreshError("affected physical key is incomplete")
        try:
            key = LedgerKey(
                int(values[0]), str(values[1]), str(values[2]), str(values[3])
            )
        except (TypeError, ValueError) as exc:
            raise BoundedStockBinRefreshError("affected physical key is malformed") from exc
        if key.item_id <= 0 or key in seen:
            raise BoundedStockBinRefreshError("affected physical keys are ambiguous")
        seen.add(key)
        result.append(key)
    return tuple(sorted(result))


def _manifest(value: BoundedPhysicalDeltaManifest | Mapping[str, Any]) -> BoundedPhysicalDeltaManifest:
    if isinstance(value, BoundedPhysicalDeltaManifest):
        result = value
    elif isinstance(value, Mapping):
        raw_backdate = value.get("backdate_from")
        if raw_backdate not in (None, "") and not isinstance(raw_backdate, datetime):
            try:
                raw_backdate = datetime.fromisoformat(str(raw_backdate))
            except ValueError as exc:
                raise BoundedStockBinRefreshError("delta manifest backdate boundary is malformed") from exc
        try:
            result = BoundedPhysicalDeltaManifest(
                new_sle_ids=tuple(int(item) for item in value.get("new_sle_ids", ())),
                supersession_edge_ids=tuple(
                    int(item) for item in value.get("supersession_edge_ids", ())
                ),
                backdate_from=raw_backdate,
            )
        except (TypeError, ValueError) as exc:
            raise BoundedStockBinRefreshError("delta manifest IDs are malformed") from exc
    else:
        raise BoundedStockBinRefreshError("bounded delta manifest is required")
    if any(item <= 0 for item in result.new_sle_ids + result.supersession_edge_ids):
        raise BoundedStockBinRefreshError("delta manifest IDs must be positive")
    if len(set(result.new_sle_ids)) != len(result.new_sle_ids):
        raise BoundedStockBinRefreshError("delta manifest contains duplicate SLE IDs")
    if len(set(result.supersession_edge_ids)) != len(result.supersession_edge_ids):
        raise BoundedStockBinRefreshError("delta manifest contains duplicate supersession IDs")
    return result


def _entry_key(entry: models.StockLedgerEntry) -> LedgerKey:
    return LedgerKey(
        int(entry.item_id), str(entry.characteristic_ref or ""),
        str(entry.organization_ref or ""), str(entry.warehouse_ref1c or ""),
    )


@dataclass(frozen=True)
class _ValidatedBoundedManifest:
    entries_by_key: dict[LedgerKey, dict[int, models.StockLedgerEntry]]
    edges_by_key: dict[LedgerKey, dict[int, models.StockLedgerFactSupersession]]


def _validate_manifest(
    db: Session,
    *,
    keys: tuple[LedgerKey, ...],
    manifest: BoundedPhysicalDeltaManifest,
    parent: models.LedgerGeneration,
    target: models.LedgerGeneration,
    target_batch_id: int,
) -> _ValidatedBoundedManifest:
    """Validate and partition one bounded manifest for all affected keys.

    The target-window completeness queries are deliberately bounded to the
    declared item IDs and then checked by the complete four-part physical key.
    This permits one manifest to cover multiple keys while rejecting an
    undeclared characteristic/organization/warehouse sharing an item ID.
    """
    lower = int(parent.physical_import_batch_id)
    upper = int(target_batch_id)
    parent_cutoff = _comparable_datetime(parent.cutoff)
    target_cutoff = _comparable_datetime(target.cutoff)
    backdate_from = _comparable_datetime(manifest.backdate_from)
    key_set = set(keys)
    item_ids = sorted({key.item_id for key in keys})
    declared_ids = set(manifest.new_sle_ids)
    declared_edge_ids = set(manifest.supersession_edge_ids)
    entries = {
        int(row.id): row
        for row in db.query(models.StockLedgerEntry)
        .filter(
            models.StockLedgerEntry.id.in_(sorted(declared_ids))
            if declared_ids else models.StockLedgerEntry.id < 0
        )
        .all()
    }
    if set(entries) != declared_ids:
        raise BoundedStockBinRefreshError("delta manifest references missing SLE")
    for row in entries.values():
        key = _entry_key(row)
        if key not in key_set:
            raise BoundedStockBinRefreshError("delta SLE is outside affected physical keys")
        if not (lower < int(row.ingest_batch_id) <= upper):
            raise BoundedStockBinRefreshError("delta SLE is outside target import boundary")
        posting_at = _comparable_datetime(row.posting_at)
        if posting_at is None or posting_at > target_cutoff:
            raise BoundedStockBinRefreshError("delta SLE is outside target cutoff")
        if posting_at <= parent_cutoff and backdate_from is None:
            raise BoundedStockBinRefreshError(
                "backdated delta requires an explicit bounded backdate boundary"
            )
        if backdate_from is not None and posting_at < backdate_from:
            raise BoundedStockBinRefreshError("delta SLE precedes declared backdate boundary")

    persisted_delta_rows = (
        db.query(models.StockLedgerEntry)
        .filter(
            models.StockLedgerEntry.ingest_batch_id > lower,
            models.StockLedgerEntry.ingest_batch_id <= upper,
            models.StockLedgerEntry.item_id.in_(item_ids) if item_ids else models.StockLedgerEntry.id < 0,
            models.StockLedgerEntry.posting_at <= target_cutoff,
        )
        .all()
    )
    persisted_delta_ids: set[int] = set()
    for row in persisted_delta_rows:
        key = _entry_key(row)
        if key not in key_set:
            raise BoundedStockBinRefreshError(
                "persisted delta row belongs to undeclared affected key"
            )
        persisted_delta_ids.add(int(row.id))
    if persisted_delta_ids != declared_ids:
        raise BoundedStockBinRefreshError("delta manifest is incomplete for affected keys")

    edges = {
        int(row.id): row
        for row in db.query(models.StockLedgerFactSupersession)
        .filter(
            models.StockLedgerFactSupersession.id.in_(sorted(declared_edge_ids))
            if declared_edge_ids else models.StockLedgerFactSupersession.id < 0
        )
        .all()
    }
    if set(edges) != declared_edge_ids:
        raise BoundedStockBinRefreshError("delta manifest references missing supersession edge")

    persisted_edge_rows = (
        db.query(models.StockLedgerFactSupersession, models.StockLedgerEntry)
        .join(
            models.StockLedgerEntry,
            models.StockLedgerEntry.id == models.StockLedgerFactSupersession.old_sle_id,
        )
        .filter(
            models.StockLedgerFactSupersession.import_batch_id > lower,
            models.StockLedgerFactSupersession.import_batch_id <= upper,
            models.StockLedgerEntry.item_id.in_(item_ids) if item_ids else models.StockLedgerEntry.id < 0,
        )
        .all()
    )
    persisted_edge_ids: set[int] = set()
    for edge, old in persisted_edge_rows:
        key = _entry_key(old)
        if key not in key_set:
            raise BoundedStockBinRefreshError(
                "persisted supersession belongs to undeclared affected key"
            )
        persisted_edge_ids.add(int(edge.id))
    if persisted_edge_ids != declared_edge_ids:
        raise BoundedStockBinRefreshError("delta manifest is incomplete for supersession edges")

    entries_by_key: dict[LedgerKey, dict[int, models.StockLedgerEntry]] = {
        key: {} for key in keys
    }
    for row in entries.values():
        entries_by_key[_entry_key(row)][int(row.id)] = row
    edges_by_key: dict[LedgerKey, dict[int, models.StockLedgerFactSupersession]] = {
        key: {} for key in keys
    }
    old_ids_by_key: dict[LedgerKey, set[int]] = {key: set() for key in keys}
    for edge in edges.values():
        if edge.old_sle_id is None:
            raise BoundedStockBinRefreshError("duplicate or missing supersession basis")
        old = db.get(models.StockLedgerEntry, int(edge.old_sle_id))
        if old is None:
            raise BoundedStockBinRefreshError("supersession old SLE is missing")
        key = _entry_key(old)
        if key not in key_set:
            raise BoundedStockBinRefreshError("supersession old SLE is outside affected keys")
        if int(edge.old_sle_id) in old_ids_by_key[key]:
            raise BoundedStockBinRefreshError("duplicate supersession basis")
        old_ids_by_key[key].add(int(edge.old_sle_id))
        edges_by_key[key][int(edge.id)] = edge
        if int(old.ingest_batch_id) > lower and int(old.id) not in declared_ids:
            raise BoundedStockBinRefreshError("supersession old delta SLE is missing from manifest")
        old_posting_at = _comparable_datetime(old.posting_at)
        if old_posting_at is None or old_posting_at > target_cutoff:
            raise BoundedStockBinRefreshError("supersession old SLE is outside target cutoff")
        if int(old.ingest_batch_id) <= lower and old_posting_at > parent_cutoff:
            raise BoundedStockBinRefreshError("supersession old SLE is outside parent basis")
        prior = (
            db.query(models.StockLedgerFactSupersession.id)
            .filter(
                models.StockLedgerFactSupersession.old_sle_id == int(old.id),
                models.StockLedgerFactSupersession.import_batch_id <= lower,
            )
            .first()
        )
        if prior is not None:
            raise BoundedStockBinRefreshError("supersession old SLE was not visible in parent basis")
        if edge.new_sle_id is not None:
            new = db.get(models.StockLedgerEntry, int(edge.new_sle_id))
            if new is None or _entry_key(new) != key:
                raise BoundedStockBinRefreshError("supersession new SLE is outside affected key")
            if int(new.id) not in declared_ids:
                raise BoundedStockBinRefreshError("supersession new SLE is missing from manifest")
    if set().union(*(set(group) for group in entries_by_key.values())) != declared_ids:
        raise BoundedStockBinRefreshError("delta manifest partition is incomplete")
    if set().union(*(set(group) for group in edges_by_key.values())) != declared_edge_ids:
        raise BoundedStockBinRefreshError("supersession manifest partition is incomplete")
    return _ValidatedBoundedManifest(entries_by_key=entries_by_key, edges_by_key=edges_by_key)


def apply_bounded_current_stock_bins(
    db: Session,
    *,
    target_generation_id: int,
    parent_generation_id: int,
    affected_physical_keys: Iterable[Any],
    delta_manifest: BoundedPhysicalDeltaManifest | Mapping[str, Any],
) -> BoundedStockBinRefreshResult:
    """Apply an explicit bounded physical delta to stable current StockBins."""
    keys = _normalise_keys(affected_physical_keys)
    delta = _manifest(delta_manifest)
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if target is None or str(target.status or "") != "building":
        raise BoundedStockBinRefreshError("target generation must be BUILDING")
    if parent is None or str(parent.status or "") != "accepted":
        raise BoundedStockBinRefreshError("parent generation must be accepted")
    if int(target.id) == int(parent.id):
        raise BoundedStockBinRefreshError("target and parent generations must differ")
    if target.cutoff is None or parent.cutoff is None:
        raise BoundedStockBinRefreshError("target and parent cutoffs are required")
    if _comparable_datetime(target.cutoff) < _comparable_datetime(parent.cutoff):
        raise BoundedStockBinRefreshError("target cutoff precedes parent cutoff")
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or int(pointer.current_generation_id or -1) != int(parent.id):
        raise BoundedStockBinRefreshError("parent generation is not current truth")
    if target.physical_import_batch_id is None or parent.physical_import_batch_id is None:
        raise BoundedStockBinRefreshError("physical import provenance is missing")
    if int(target.physical_import_batch_id) < int(parent.physical_import_batch_id):
        raise BoundedStockBinRefreshError("target physical boundary precedes parent")

    try:
        parent_batch = require_import_batch(db, int(parent.physical_import_batch_id))
        target_batch = require_import_batch(db, int(target.physical_import_batch_id))
    except PhysicalVisibilityError as exc:
        raise BoundedStockBinRefreshError(str(exc)) from exc
    if (
        _comparable_datetime(parent_batch.cutoff) != _comparable_datetime(parent.cutoff)
        or _comparable_datetime(target_batch.cutoff) != _comparable_datetime(target.cutoff)
    ):
        raise BoundedStockBinRefreshError(
            "generation cutoff does not match physical import boundary"
        )

    if not keys:
        if delta.new_sle_ids or delta.supersession_edge_ids:
            raise BoundedStockBinRefreshError("delta manifest is outside empty affected scope")
        return BoundedStockBinRefreshResult(
            parent_generation_id=int(parent.id), target_generation_id=int(target.id),
            affected_keys=(), changed_keys=(), semantic_noop_keys=(), delta_rows=0,
        )

    validated = _validate_manifest(
        db,
        keys=keys,
        manifest=delta,
        parent=parent,
        target=target,
        target_batch_id=int(target.physical_import_batch_id),
    )
    changed: list[LedgerKey] = []
    noops: list[LedgerKey] = []
    delta_rows = sum(len(rows) for rows in validated.entries_by_key.values())
    # Lock/read the compact owners first, then resolve every provenance
    # pointer in one bounded query.  The old per-key ``db.get`` made a
    # prod-sized refresh spend most of its time reloading the same
    # StockLedgerEntry lineage inside this loop.
    current_by_key: dict[LedgerKey, models.StockBin | None] = {}
    last_entry_ids: set[int] = set()
    current_rows = (
        db.query(models.StockBin)
        .filter(
            models.StockBin.is_current.is_(True),
            tuple_(
                models.StockBin.item_id,
                models.StockBin.characteristic_ref,
                models.StockBin.organization_ref,
                models.StockBin.warehouse_ref1c,
            ).in_([
                (
                    key.item_id,
                    key.characteristic_ref,
                    key.organization_ref,
                    key.warehouse_ref1c,
                )
                for key in keys
            ]),
        )
        .with_for_update()
        .all()
    )
    current_rows_by_key: dict[LedgerKey, list[models.StockBin]] = {
        key: [] for key in keys
    }
    for row in current_rows:
        row_key = LedgerKey(
            int(row.item_id),
            str(row.characteristic_ref or ""),
            str(row.organization_ref or ""),
            str(row.warehouse_ref1c or ""),
        )
        if row_key in current_rows_by_key:
            current_rows_by_key[row_key].append(row)
    for key in keys:
        matching_rows = current_rows_by_key[key]
        if len(matching_rows) > 1:
            raise BoundedStockBinRefreshError("current StockBin owner is ambiguous")
        current = matching_rows[0] if matching_rows else None
        if current is not None and int(current.ledger_generation_id) not in {
            int(parent.id), int(target.id)
        }:
            raise BoundedStockBinRefreshError("current StockBin owner is stale")
        current_by_key[key] = current
        if current is not None and current.last_entry_id is not None:
            last_entry_ids.add(int(current.last_entry_id))

    last_entries = {
        int(row.id): row
        for row in (
            db.query(models.StockLedgerEntry)
            .filter(models.StockLedgerEntry.id.in_(sorted(last_entry_ids)))
            .all()
            if last_entry_ids
            else []
        )
    }
    if set(last_entries) != last_entry_ids:
        missing = sorted(last_entry_ids - set(last_entries))
        raise BoundedStockBinRefreshError(
            f"current last entry is missing: {missing}"
        )

    old_ids_by_key = {
        key: {int(edge.old_sle_id) for edge in validated.edges_by_key[key].values()}
        for key in keys
    }
    unresolved_old_ids = set().union(*(
        old_ids_by_key[key].difference(validated.entries_by_key[key])
        for key in keys
    ))
    old_basis_rows = {
        int(row.id): row
        for row in (
            db.query(models.StockLedgerEntry)
            .filter(models.StockLedgerEntry.id.in_(sorted(unresolved_old_ids)))
            .all()
            if unresolved_old_ids
            else []
        )
    }

    for key in keys:
        current = current_by_key[key]
        if (
            current is not None
            and int(current.ledger_generation_id) == int(parent.id)
            and current.last_entry_id is not None
        ):
            current_last = last_entries[int(current.last_entry_id)]
            if (
                current_last is None
                or _entry_key(current_last) != key
                or int(current_last.ingest_batch_id) > int(parent.physical_import_batch_id)
                or current_last.posting_at is None
                or _comparable_datetime(current_last.posting_at)
                > _comparable_datetime(parent.cutoff)
            ):
                raise BoundedStockBinRefreshError("current last entry is outside parent basis")
        entries = validated.entries_by_key[key]
        edges = validated.edges_by_key[key]

        if not entries and not edges:
            noops.append(key)
            continue

        if current is not None and int(current.ledger_generation_id) == int(target.id):
            noops.append(key)
            continue

        old_ids = old_ids_by_key[key]
        old_rows: dict[int, models.StockLedgerEntry] = {}
        for old_id in old_ids:
            old = entries.get(old_id) or old_basis_rows.get(old_id)
            if old is None:
                raise BoundedStockBinRefreshError("supersession basis SLE is missing")
            old_rows[old_id] = old
        base = Decimal(str(current.on_hand or 0)) if current is not None else Decimal("0")
        _, new_qty = fold_running_balance(row.qty for row in entries.values())
        _, superseded_qty = fold_running_balance(row.qty for row in old_rows.values())
        _, next_qty = fold_running_balance((new_qty, -superseded_qty), start=base)

        last_id = current.last_entry_id if current is not None else None
        candidates = [row for row in entries.values() if int(row.id) not in old_ids]
        latest = max(
            candidates,
            key=lambda row: (_comparable_datetime(row.posting_at), int(row.id)),
        ) if candidates else None
        current_last_was_superseded = (
            current is not None
            and current.last_entry_id is not None
            and int(current.last_entry_id) in old_ids
        )
        if current_last_was_superseded:
            # The compact owner stores only the last visible ID.  If a
            # correction removes it, a replacement earlier than that ID would
            # require discovering an unaffected predecessor from history.  Do
            # not silently publish an invalid last_entry_id; the caller must
            # provide a wider bounded scope or use maintenance recovery.
            if latest is None:
                raise BoundedStockBinRefreshError(
                    "superseded current last entry lacks bounded replacement"
                )
            previous = last_entries.get(int(current.last_entry_id))
            if previous is None:
                raise BoundedStockBinRefreshError("current last entry is missing")
            if (
                _comparable_datetime(latest.posting_at), int(latest.id)
            ) < (
                _comparable_datetime(previous.posting_at), int(previous.id)
            ):
                raise BoundedStockBinRefreshError(
                    "bounded correction cannot prove replacement last entry"
                )
            last_id = int(latest.id)
        elif latest is not None:
            if last_id is None:
                last_id = int(latest.id)
            else:
                previous = last_entries.get(int(last_id))
                if previous is None:
                    raise BoundedStockBinRefreshError("current last entry is missing")
                if (
                    _comparable_datetime(latest.posting_at), int(latest.id)
                ) > (
                    _comparable_datetime(previous.posting_at), int(previous.id)
                ):
                    last_id = int(latest.id)

        if current is None:
            current = models.StockBin(
                ledger_generation_id=int(target.id), item_id=key.item_id,
                characteristic_ref=key.characteristic_ref,
                organization_ref=key.organization_ref,
                warehouse_ref1c=key.warehouse_ref1c,
                on_hand=next_qty, last_entry_id=last_id, is_current=True,
            )
            db.add(current)
        else:
            current.ledger_generation_id = int(target.id)
            current.on_hand = next_qty
            current.last_entry_id = last_id
            current.is_current = True
        changed.append(key)

    db.flush()
    from app.services.mrp_stock_helpers import (
        invalidate_current_stock_bin_provenance_cache,
    )

    invalidate_current_stock_bin_provenance_cache(db)
    return BoundedStockBinRefreshResult(
        parent_generation_id=int(parent.id), target_generation_id=int(target.id),
        affected_keys=keys, changed_keys=tuple(changed),
        semantic_noop_keys=tuple(noops), delta_rows=delta_rows,
    )
