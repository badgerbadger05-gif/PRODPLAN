"""R10 RED contracts for removing runtime PlanningRead architecture."""

from datetime import datetime, timezone
from pathlib import Path

from app import models
from app.services.item_ledger.current_execution import (
    get_current_execution_scope,
    load_current_execution_rows,
    publish_current_execution_scope,
)


REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend" / "app"


def _python_sources():
    return (
        path for path in BACKEND.rglob("*.py")
        if path.name != "models.py"
    )


def test_runtime_backend_has_no_legacy_planning_read_symbols():
    forbidden = (
        "PlanningReadSnapshot",
        "PlanningReadRow",
        "PlanningReadRootMember",
        "get_latest_read_snapshot",
        "publish_read_snapshot",
    )
    offenders = {
        path.relative_to(REPO).as_posix(): [token for token in forbidden if token in path.read_text(encoding="utf-8")]
        for path in _python_sources()
        if any(token in path.read_text(encoding="utf-8") for token in forbidden)
    }
    assert offenders == {}


def test_runtime_consumers_use_projection_owner_modules_only():
    old_modules = (
        "purchase_control_snapshot.py",
        "production_control_journal_snapshot.py",
        "mrp_result_snapshot.py",
    )
    new_modules = (
        "purchase_control_projection.py",
        "production_control_journal_projection.py",
        "mrp_result_projection.py",
    )
    service_dir = BACKEND / "services"
    assert all(not (service_dir / name).exists() for name in old_modules)
    assert all((service_dir / name).exists() for name in new_modules)

    runtime_sources = [
        BACKEND / "routers",
        BACKEND / "services",
    ]
    stale_imports = {
        path.relative_to(REPO).as_posix()
        for root in runtime_sources
        for path in root.rglob("*.py")
        if "_snapshot" in path.read_text(encoding="utf-8")
        and path.name not in {"models.py"}
    }
    assert stale_imports == set()


def test_truth_and_material_availability_export_no_legacy_reader_apis():
    planning_truth = (BACKEND / "services" / "planning_truth.py").read_text(encoding="utf-8")
    material_availability = (BACKEND / "services" / "production_control_material_availability.py").read_text(encoding="utf-8")
    current_execution = (BACKEND / "services" / "item_ledger" / "current_execution.py").read_text(encoding="utf-8")
    assert "get_latest_read_snapshot" not in planning_truth
    assert "publish_read_snapshot" not in planning_truth
    assert "get_latest_read_snapshot" not in material_availability
    assert "PlanningReadRow" not in material_availability
    assert "publish_current_obligation_views_from_snapshots" not in current_execution


def test_legacy_converter_is_tools_only_and_does_not_import_orm_models():
    adapter = REPO / "backend" / "tools" / "current_execution_legacy_adapter.py"
    migration = REPO / "backend" / "tools" / "current_execution_migration.py"
    assert adapter.exists()
    adapter_source = adapter.read_text(encoding="utf-8")
    assert "PlanningReadSnapshot" not in adapter_source
    assert "PlanningReadRow" not in adapter_source
    assert "PlanningReadRootMember" not in adapter_source
    assert "from app import models" not in adapter_source
    assert "current_execution_legacy_adapter" in migration.read_text(encoding="utf-8")


def test_current_runtime_reader_reads_current_scope_and_rows(db_session):
    physical = models.PhysicalImportBatch(
        batch_key="r10-runtime-owner-physical",
        status="completed",
        cutoff=datetime(2026, 9, 1, tzinfo=timezone.utc),
        source_watermarks={},
        completed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    generation = models.LedgerGeneration(
        generation_key="r10-runtime-owner-generation",
        status="accepted",
        cutoff=physical.cutoff,
        accepted_at=physical.cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="tests/r10-runtime-owner",
    )
    db_session.add_all([physical, generation])
    db_session.flush()

    publish_current_execution_scope(
        db_session,
        source_revision="accepted:r10-runtime-owner",
        source_generation_id=int(generation.id),
        scope_key="r10-runtime-owner",
        entity_kinds=("runtime_probe",),
        rows=[{
            "entity_kind": "runtime_probe",
            "business_identity": "probe:1",
            "scope_key": "r10-runtime-owner",
            "payload": {"value": 7},
        }],
    )

    scope = get_current_execution_scope(
        db_session,
        entity_kind="runtime_probe",
        scope_key="r10-runtime-owner",
    )
    rows = load_current_execution_rows(
        db_session,
        entity_kind="runtime_probe",
        scope_key="r10-runtime-owner",
    )
    assert scope is not None
    assert scope.source_generation_id == generation.id
    assert len(rows) == 1
    assert rows[0].business_identity == "probe:1"
    assert rows[0].payload == {"value": 7}
