"""The future-supply reader never falls back from missing truth."""

from datetime import datetime

import pytest

from app import models
from app.services.item_ledger.future_supply_read import (
    FutureSupplyUnavailable,
    future_supply_model,
)


def test_accepted_generation_without_current_pointer_is_unavailable(db_session):
    physical = models.PhysicalImportBatch(
        batch_key="r7-read-physical", status="completed",
        cutoff=datetime(2026, 9, 1), source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r7-read-generation", status="accepted",
        cutoff=datetime(2026, 9, 1), physical_import_batch=physical,
        algorithm_version="test", source_watermarks={}, capabilities={},
    )
    db_session.add(generation)
    db_session.flush()

    with pytest.raises(FutureSupplyUnavailable, match="pointer"):
        future_supply_model(db_session, int(generation.id))


def test_building_generation_selects_staging_owner(db_session):
    physical = models.PhysicalImportBatch(
        batch_key="r7-read-building-physical", status="completed",
        cutoff=datetime(2026, 9, 1), source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r7-read-building-generation", status="building",
        cutoff=datetime(2026, 9, 1), physical_import_batch=physical,
        algorithm_version="test", source_watermarks={}, capabilities={},
    )
    db_session.add(generation)
    db_session.flush()

    assert future_supply_model(db_session, int(generation.id)) is models.LedgerFutureSupply
