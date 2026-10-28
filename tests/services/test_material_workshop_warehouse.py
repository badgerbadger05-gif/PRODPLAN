"""Regression: workshop-warehouse name lookup must use warehouse_ref1c, not the PK.

WorkshopWarehouseBinding stores the 1С business key (warehouse_ref1c) and has
no warehouse_name column, so _material_workshop_warehouse backfills the name
from StockWarehouse. A primary-key ``db.get(StockWarehouse, ref)`` with the
GUID makes Postgres raise ``InvalidTextRepresentation`` (invalid input syntax
for type integer) and kills the material-availability publication for the
executor line.
"""
import types

from app import models
from app.services import workshop_resolution
from app.services.production_control_material_availability import (
    _material_workshop_warehouse,
)

WAREHOUSE_REF = "dbd29eb2-bae2-11f0-885d-9ee51454587f"
WAREHOUSE_NAME = "Склад №2 - склад механического цеха"


def _pin_workshop_resolution(monkeypatch, binding):
    monkeypatch.setattr(
        workshop_resolution,
        "resolve_workshop_for_product",
        lambda db, product, spec_id=None: 5,
    )
    monkeypatch.setattr(
        workshop_resolution,
        "warehouse_binding_for_workshop",
        lambda db, workshop_id: binding,
    )


def test_workshop_warehouse_name_resolved_by_ref1c(db_session, monkeypatch):
    db = db_session
    db.add(models.StockWarehouse(
        warehouse_ref1c=WAREHOUSE_REF,
        warehouse_name=WAREHOUSE_NAME,
    ))
    db.commit()
    binding = models.WorkshopWarehouseBinding(
        workshop_id=5,
        warehouse_ref1c=WAREHOUSE_REF,
        production_warehouse_ref1c=None,
    )
    _pin_workshop_resolution(monkeypatch, binding)

    product = types.SimpleNamespace(product_id=21190)
    ref, name = _material_workshop_warehouse(
        db, product, custody_product_id=21190, spec_id=None,
    )

    assert ref == WAREHOUSE_REF
    assert name == WAREHOUSE_NAME


def test_workshop_warehouse_name_empty_when_ref_unknown(db_session, monkeypatch):
    db = db_session
    binding = models.WorkshopWarehouseBinding(
        workshop_id=5,
        warehouse_ref1c=WAREHOUSE_REF,
        production_warehouse_ref1c=None,
    )
    _pin_workshop_resolution(monkeypatch, binding)

    product = types.SimpleNamespace(product_id=21190)
    ref, name = _material_workshop_warehouse(
        db, product, custody_product_id=21190, spec_id=None,
    )

    assert ref == WAREHOUSE_REF
    assert name == ""
