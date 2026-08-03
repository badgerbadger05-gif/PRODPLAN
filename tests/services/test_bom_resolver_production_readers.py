from datetime import datetime, timezone

from app import models
from app.services.planning_service import _bom_descendant_ids_for_roots


def _item(db, code: str) -> models.Item:
    row = models.Item(
        item_code=code,
        item_name=code,
        item_ref1c=f"item-ref-{code}",
        replenishment_method="Производство",
        unit="шт",
        status="active",
    )
    db.add(row)
    db.flush()
    return row


def _spec(
    db,
    ref: str,
    *,
    kind: models.ProductionKind | None = None,
) -> models.Specification:
    row = models.Specification(
        spec_name=ref,
        spec_ref1c=ref,
        production_kind_id=kind.id if kind else None,
    )
    db.add(row)
    db.flush()
    return row


def test_production_readers_follow_pinned_child_spec(
    db_session,
    building_ledger_generation,
):
    now = datetime.now(timezone.utc)
    building_ledger_generation.status = "accepted"
    building_ledger_generation.cutoff = now
    building_ledger_generation.accepted_at = now
    db_session.flush()
    root = _item(db_session, "READER-ROOT")
    child = _item(db_session, "READER-CHILD")
    pinned_leaf = _item(db_session, "READER-PINNED-LEAF")
    default_leaf = _item(db_session, "READER-DEFAULT-LEAF")

    root_kind = models.ProductionKind(ref_1c="kind-root", name="Root kind")
    pinned_kind = models.ProductionKind(ref_1c="kind-pinned", name="Pinned kind")
    default_kind = models.ProductionKind(ref_1c="kind-default", name="Default kind")
    db_session.add_all([root_kind, pinned_kind, default_kind])
    db_session.flush()

    root_resource = models.ProductionResource(resource_name="Root resource")
    pinned_resource = models.ProductionResource(resource_name="Pinned resource")
    default_resource = models.ProductionResource(resource_name="Default resource")
    db_session.add_all([root_resource, pinned_resource, default_resource])
    db_session.flush()
    db_session.add_all(
        [
            models.ResourceProductionKind(
                resource_id=root_resource.resource_id,
                production_kind_id=root_kind.id,
            ),
            models.ResourceProductionKind(
                resource_id=pinned_resource.resource_id,
                production_kind_id=pinned_kind.id,
            ),
            models.ResourceProductionKind(
                resource_id=default_resource.resource_id,
                production_kind_id=default_kind.id,
            ),
        ]
    )

    root_spec = _spec(db_session, "reader-root-spec", kind=root_kind)
    pinned_spec = _spec(db_session, "reader-pinned-spec", kind=pinned_kind)
    default_spec = _spec(db_session, "reader-default-spec", kind=default_kind)
    db_session.add_all(
        [
            models.DefaultSpecification(
                item_id=root.item_id,
                spec_id=root_spec.spec_id,
            ),
            models.DefaultSpecification(
                item_id=child.item_id,
                spec_id=default_spec.spec_id,
            ),
            models.SpecComponent(
                spec_id=root_spec.spec_id,
                item_id=child.item_id,
                quantity=2,
                component_type="Сборка",
                component_spec_ref1c=pinned_spec.spec_ref1c,
            ),
            models.SpecComponent(
                spec_id=pinned_spec.spec_id,
                item_id=pinned_leaf.item_id,
                quantity=3,
            ),
            models.SpecComponent(
                spec_id=default_spec.spec_id,
                item_id=default_leaf.item_id,
                quantity=5,
            ),
        ]
    )
    db_session.flush()

    descendants = _bom_descendant_ids_for_roots(db_session, [root.item_id])
    assert pinned_leaf.item_id in descendants
    assert default_leaf.item_id not in descendants
