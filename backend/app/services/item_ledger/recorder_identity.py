"""Neutral recorder-to-order identity resolution for accepted Ledger facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from sqlalchemy.orm import Session

from app import models


MANUFACTURE_DOCTYPE = "manufacture"
MATERIAL_ISSUE_DOCTYPE = "material_issue"
SYNC_LINK_FACT_STATUSES = frozenset({"success", "posted"})
_IN_CHUNK = 500


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _chunks(values: Sequence[Any]) -> Iterator[list[Any]]:
    for start in range(0, len(values), _IN_CHUNK):
        yield list(values[start : start + _IN_CHUNK])


@dataclass
class RecorderIdentityIndex:
    exact_product_ids: dict[str, set[int]] = field(default_factory=dict)
    order_ids: dict[str, set[int]] = field(default_factory=dict)


def _sync_link_sources(
    db: Session,
    recorder_refs: Sequence[str],
    doctype: str,
) -> dict[str, set[int]]:
    sources: dict[str, set[int]] = {}
    for chunk in _chunks(recorder_refs):
        rows = (
            db.query(models.SyncLink.target_ref_key, models.SyncLink.source_id)
            .filter(
                models.SyncLink.target_ref_key.in_(chunk),
                models.SyncLink.status.in_(SYNC_LINK_FACT_STATUSES),
                models.SyncLink.source_doctype == doctype,
            )
            .all()
        )
        for target_ref, source_id in rows:
            sources.setdefault(_norm(target_ref), set()).add(int(source_id))
    return sources


def build_recorder_identity_index(
    db: Session,
    recorder_refs: Sequence[str],
) -> RecorderIdentityIndex:
    """Resolve recorder references without allocating any physical quantity."""
    index = RecorderIdentityIndex()
    refs = sorted({_norm(ref) for ref in recorder_refs if _norm(ref)})
    if not refs:
        return index

    for chunk in _chunks(refs):
        rows = (
            db.query(
                models.ProductionManufacture.exported_ref1c,
                models.ProductionManufacture.product_id,
            )
            .filter(models.ProductionManufacture.exported_ref1c.in_(chunk))
            .all()
        )
        for exported_ref, product_id in rows:
            index.exact_product_ids.setdefault(_norm(exported_ref), set()).add(
                int(product_id)
            )

    manufacture_links = _sync_link_sources(db, refs, MANUFACTURE_DOCTYPE)
    manufacture_ids = sorted({value for values in manufacture_links.values() for value in values})
    product_by_manufacture: dict[int, int] = {}
    for chunk in _chunks(manufacture_ids):
        rows = (
            db.query(
                models.ProductionManufacture.manufacture_id,
                models.ProductionManufacture.product_id,
            )
            .filter(models.ProductionManufacture.manufacture_id.in_(chunk))
            .all()
        )
        product_by_manufacture.update(
            {int(manufacture_id): int(product_id) for manufacture_id, product_id in rows}
        )
    for recorder_ref, ids in manufacture_links.items():
        resolved = {
            product_by_manufacture[value]
            for value in ids
            if value in product_by_manufacture
        }
        if resolved:
            index.exact_product_ids.setdefault(recorder_ref, set()).update(resolved)

    order_refs_by_recorder: dict[str, set[str]] = {ref: {ref} for ref in refs}
    for chunk in _chunks(refs):
        rows = (
            db.query(
                models.StockRecorderPull.recorder_ref,
                models.StockRecorderPull.order_ref,
            )
            .filter(
                models.StockRecorderPull.recorder_ref.in_(chunk),
                models.StockRecorderPull.order_ref.isnot(None),
            )
            .all()
        )
        for recorder_ref, order_ref in rows:
            if _norm(order_ref):
                order_refs_by_recorder.setdefault(_norm(recorder_ref), set()).add(
                    _norm(order_ref)
                )

    all_order_refs = sorted({value for values in order_refs_by_recorder.values() for value in values})
    order_id_by_ref: dict[str, int] = {}
    for chunk in _chunks(all_order_refs):
        rows = (
            db.query(models.ProductionOrder.order_ref1c, models.ProductionOrder.order_id)
            .filter(models.ProductionOrder.order_ref1c.in_(chunk))
            .all()
        )
        order_id_by_ref.update({_norm(order_ref): int(order_id) for order_ref, order_id in rows})
    for recorder_ref, order_refs in order_refs_by_recorder.items():
        resolved = {order_id_by_ref[value] for value in order_refs if value in order_id_by_ref}
        if resolved:
            index.order_ids.setdefault(recorder_ref, set()).update(resolved)

    issue_links = _sync_link_sources(db, refs, MATERIAL_ISSUE_DOCTYPE)
    issue_ids = sorted({value for values in issue_links.values() for value in values})
    order_by_issue: dict[int, int] = {}
    for chunk in _chunks(issue_ids):
        rows = (
            db.query(
                models.ProductionMaterialIssue.issue_id,
                models.ProductionMaterialIssue.order_id,
            )
            .filter(models.ProductionMaterialIssue.issue_id.in_(chunk))
            .all()
        )
        order_by_issue.update({int(issue_id): int(order_id) for issue_id, order_id in rows})
    for recorder_ref, ids in issue_links.items():
        resolved = {order_by_issue[value] for value in ids if value in order_by_issue}
        if resolved:
            index.order_ids.setdefault(recorder_ref, set()).update(resolved)

    return index
