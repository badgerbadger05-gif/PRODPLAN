"""Explicit accepted Ledger context for legacy DBR service scenarios."""

from datetime import datetime

import pytest

from app.models import LedgerGeneration, PhysicalImportBatch, PlanningTruthState
from app.services.dbr import (
    drum_service,
    feeder_position_service,
    feeder_signal_service,
)


@pytest.fixture(autouse=True)
def _diagnostic_dbr_generation(db_session, monkeypatch):
    batch = PhysicalImportBatch(
        batch_key="dbr-diagnostic",
        status="completed",
        cutoff=datetime(2026, 7, 23),
        source_watermarks={"source": "test-diagnostic"},
        completed_at=datetime(2026, 7, 23),
    )
    generation = LedgerGeneration(
        generation_key="dbr-diagnostic",
        status="accepted",
        cutoff=datetime(2026, 7, 23),
        source_watermarks={},
        capabilities={},
        physical_import_batch=batch,
        algorithm_version="test/diagnostic",
        accepted_at=datetime(2026, 7, 23),
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(
        PlanningTruthState(id=1, current_generation_id=generation.id)
    )
    db_session.flush()

    def attach_lineage(db):
        """Legacy fixtures belong to this explicit diagnostic generation."""
        if not hasattr(db, "query"):
            return
        for model in (
            drum_service.DbrDrumSchedule,
            feeder_position_service.DbrSupermarketPosition,
            feeder_signal_service.DbrFeederSignal,
        ):
            db.query(model).filter(
                model.ledger_generation_id.is_(None)
            ).update(
                {"ledger_generation_id": generation.id},
                synchronize_session=False,
            )
        db.flush()

    build_schedule = drum_service.build_schedule
    rebuild_positions = feeder_position_service.rebuild_positions
    preview_positions = feeder_position_service.preview_positions
    refresh_signals = feeder_signal_service.refresh_signals
    preview_signals = feeder_signal_service.preview_signals

    monkeypatch.setattr(
        drum_service,
        "build_schedule",
        lambda db, program_id, **kwargs: (
            attach_lineage(db),
            build_schedule(
                db,
                program_id,
                ledger_generation_id=kwargs.pop(
                    "ledger_generation_id", generation.id
                ),
                **kwargs,
            ),
        )[1],
    )
    monkeypatch.setattr(
        feeder_position_service,
        "rebuild_positions",
        lambda db, *args, **kwargs: (
            attach_lineage(db),
            rebuild_positions(
                db,
                *args,
                ledger_generation_id=kwargs.pop(
                    "ledger_generation_id", generation.id
                ),
                diagnostic_legacy=kwargs.pop("diagnostic_legacy", True),
                **kwargs,
            ),
        )[1],
    )
    monkeypatch.setattr(
        feeder_position_service,
        "preview_positions",
        lambda db, *args, **kwargs: (
            attach_lineage(db),
            preview_positions(
                db,
                *args,
                ledger_generation_id=kwargs.pop(
                    "ledger_generation_id", generation.id
                ),
                diagnostic_legacy=kwargs.pop("diagnostic_legacy", True),
                **kwargs,
            ),
        )[1],
    )
    monkeypatch.setattr(
        feeder_signal_service,
        "refresh_signals",
        lambda db, *args, **kwargs: (
            attach_lineage(db),
            refresh_signals(
                db,
                *args,
                ledger_generation_id=kwargs.pop(
                    "ledger_generation_id", generation.id
                ),
                diagnostic_legacy=kwargs.pop("diagnostic_legacy", True),
                **kwargs,
            ),
        )[1],
    )
    monkeypatch.setattr(
        feeder_signal_service,
        "preview_signals",
        lambda db, *args, **kwargs: (
            attach_lineage(db),
            preview_signals(
                db,
                *args,
                ledger_generation_id=kwargs.pop(
                    "ledger_generation_id", generation.id
                ),
                diagnostic_legacy=kwargs.pop("diagnostic_legacy", True),
                **kwargs,
            ),
        )[1],
    )
