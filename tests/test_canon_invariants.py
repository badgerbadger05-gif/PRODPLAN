"""Mechanical governance checks for the active PRODPLAN canon.

These checks prevent the normative documents from silently drifting back to
deleted contracts or retired calculation semantics. Runtime ownership checks
listed in .docs/CANON.md are added here as the corresponding migration lands.
"""

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PRODPLAN = REPO.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").lstrip("\ufeff")


def _python_sources(root: Path):
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def _enclosing_call_sites(
    path: Path,
    predicate,
) -> set[tuple[str, str, str]]:
    tree = ast.parse(_read(path), filename=str(path))
    relative = path.relative_to(REPO).as_posix()
    sites: set[tuple[str, str, str]] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.functions: list[str] = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            label = predicate(node)
            if label is not None:
                sites.add(
                    (
                        relative,
                        self.functions[-1] if self.functions else "<module>",
                        label,
                    )
                )
            self.generic_visit(node)

    Visitor().visit(tree)
    return sites


def test_canon_keeps_governance_mechanisms() -> None:
    canon = _read(REPO / ".docs/CANON.md")

    assert "mrp-decisions-log.md" in canon
    assert "Реестр канонических модулей" in canon
    assert "tests/test_canon_invariants.py" in canon


def test_canonical_module_registry_points_to_existing_code() -> None:
    required_paths = (
        "backend/app/services/item_ledger/ingest.py",
        "backend/app/services/item_ledger/physical.py",
        "backend/app/services/item_ledger/physical_visibility.py",
        "backend/app/services/item_ledger/reservation.py",
        "backend/app/services/item_ledger/reservation_ledger.py",
        "backend/app/services/mrp_freeze.py",
        "backend/app/services/planning_service.py",
        "backend/app/services/item_ledger/generation_lifecycle.py",
        "backend/app/services/obligation_refresh_orchestrator.py",
        "backend/app/services/planning_truth.py",
    )

    missing = [path for path in required_paths if not (REPO / path).is_file()]
    assert not missing, f"CANON points to missing modules: {missing}"


def test_execution_contract_uses_percent_and_caps_execution() -> None:
    decisions = _read(PRODPLAN / "mrp-decisions-log.md")
    reservation = _read(REPO / ".docs/reservation-replenishment-core.md")

    for document in (decisions, reservation):
        assert "replenishment_required_qty * 100" in document
        assert "min(" in document
        assert "replenishment_received_qty" in document
    assert "от 0 до 100" in decisions
    assert "0 <= execution_pct <= 100" in reservation


def test_plan_output_field_name_is_canonical() -> None:
    design = _read(PRODPLAN / "mrp-item-ledger-design.md")
    assembly = _read(REPO / ".docs/assembly-queue-and-drum.md")

    assert "accepted_plan_output_qty" in design
    assert "accepted_plan_output_qty" in assembly
    assert "accepted_output_qty" not in design
    assert "accepted_output_qty" not in assembly


def test_user_guide_does_not_restore_retired_workflows() -> None:
    guide = _read(REPO / "docs/user_instruction_prodplan.md")
    retired_phrases = (
        "Выпуск недельный",
        "пересобирается снимок",
        "MRP по активным заказам",
        "фиксировать выпуск кнопкой",
    )

    found = [phrase for phrase in retired_phrases if phrase in guide]
    assert not found, f"Retired workflow returned to user guide: {found}"


def test_active_sources_do_not_reference_deleted_contracts() -> None:
    forbidden_names = (
        "mrp-ledger-blueprint-v2.md",
        "dbr_parallel_module_roadmap.md",
        "planning_comparison_batch",
        "planning_comparison_event",
        "planning_comparison_snapshot",
        "planning_comparison_row",
        "planning_comparison_diff",
        "mrp_execution_allocation",
        "mrp_requirement_carry",
        "mrp_drift_event",
    )
    roots = (
        REPO / "backend/app",
        REPO / ".docs",
        REPO / "docs",
    )
    offenders: list[str] = []

    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".md"}:
                continue
            text = _read(path)
            if any(name in text for name in forbidden_names):
                offenders.append(str(path.relative_to(PRODPLAN)))

    assert not offenders, f"References to deleted contracts: {offenders}"


def test_manual_dbr_runtime_cannot_return() -> None:
    assert not (REPO / "backend/app/routers/dbr.py").exists()
    assert not list((REPO / "backend/app/services/dbr").glob("**/*.py"))

    models = _read(REPO / "backend/app/models.py")
    forbidden_models = (
        "DbrSettings",
        "DbrCategorySupplyRisk",
        "DbrSupermarketPosition",
        "DbrFeederSignal",
        "DbrProductionProgram",
        "DbrDrumSchedule",
        "DbrDrumSlot",
        "DbrDrumCapacityGap",
        "source_dbr_signal_id",
    )
    found = [name for name in forbidden_models if name in models]
    assert not found, f"Legacy manual DBR models returned: {found}"
    assert "class AssemblyRate" in models
    assert '__tablename__ = "dbr_assembly_rate"' in models


def test_frontend_ui_must_not_call_fetch_directly() -> None:
    ui_root = REPO / "frontend-erp-shell/src/ui"
    offenders = [
        path.relative_to(PRODPLAN).as_posix()
        for path in ui_root.rglob("*.tsx")
        if (path.is_file() and "fetch(" in _read(path))
    ]
    assert not offenders, f"Direct fetch calls in ui layer are prohibited: {offenders}"


def test_floating_reservation_projection_cannot_return() -> None:
    models = _read(REPO / "backend/app/models.py")
    reservation_runtime = _read(
        REPO / "backend/app/services/item_ledger/reservation_ledger.py"
    )
    forbidden = (
        "class ReservationCoverage",
        '__tablename__ = "reservation_coverage"',
        "covered_on_hand_qty",
        "covered_incoming_supplier_qty",
        "covered_incoming_wip_qty",
        "uncovered_qty",
        "coverage_state",
        "redistribute",
        'CONSUME = "consume"',
    )
    found = [
        token
        for token in forbidden
        if token in models or token in reservation_runtime
    ]
    assert not found, f"Legacy floating reservation projection returned: {found}"


def test_local_production_flow_endpoints_are_canonical() -> None:
    production_router = _read(REPO / "backend/app/routers/production_control.py")

    expected_router_tokens = (
        "post_produce_line",
        "post_return_leftovers",
        "post_export_manufactures_to_1c",
        "post_rollback_local_manufacture",
        "post_export_piecework_to_1c",
        "/orders/{product_id}/produce",
        "/orders/{product_id}/return-leftovers",
        "/manufactures/export-to-1c",
        "/manufactures/{manufacture_id}/rollback-local",
        "/manufactures/export-piecework-to-1c",
    )
    missing_router_tokens = [
        token for token in expected_router_tokens if token not in production_router
    ]
    assert not missing_router_tokens, (
        f"canonical production-control endpoints missing in router: {missing_router_tokens}"
    )

    # Frontend may be in transition across releases; enforce canonical backend
    # contract at minimum.


def test_frozen_requirement_has_no_execution_caches_or_mutating_engine() -> None:
    models = _read(REPO / "backend/app/models.py")
    requirement_block = models.split("class MrpRequirement(Base):", 1)[1].split(
        "class MrpRequirementBucket(Base):", 1
    )[0]
    forbidden_columns = (
        "covered_qty = Column",
        "remaining_qty = Column",
        "executed_qty = Column",
        "carried_remaining = Column",
        "initial_snapshot_stock = Column",
        "drift_adjustment_qty = Column",
    )
    assert not [
        token for token in forbidden_columns if token in requirement_block
    ]
    assert not (REPO / "backend/app/services/mrp_reconciliation.py").exists()
    assert "class ProductionManufacture(" in models
    assert (REPO / "backend/app/services/one_c_manufacture_export.py").exists()
    assert (REPO / "backend/app/services/one_c_piecework_export.py").exists()
    production_router = _read(REPO / "backend/app/routers/production_control.py")
    assert "return-leftovers" in production_router
    purchase_journal = _read(
        REPO / "backend/app/services/purchase_control_journal.py"
    )
    purchase_snapshot = _read(
        REPO / "backend/app/services/purchase_control_snapshot.py"
    )
    assert "PlannedPurchase" not in purchase_journal
    assert "ReplenishmentWorkItem" in purchase_snapshot
    assert "create_production_orders_from_mrp_requirements" not in production_router


def test_reservation_event_writer_registry_is_exact() -> None:
    def reservation_event_constructor(node: ast.Call) -> str | None:
        target = node.func
        if isinstance(target, ast.Attribute):
            if target.attr == "ReservationEvent":
                return "ReservationEvent"
        elif isinstance(target, ast.Name) and target.id == "ReservationEvent":
            return "ReservationEvent"
        return None

    actual: set[tuple[str, str, str]] = set()
    for path in _python_sources(REPO / "backend/app"):
        actual |= _enclosing_call_sites(path, reservation_event_constructor)

    assert actual == {
        (
            "backend/app/services/item_ledger/reservation.py",
            "append_realization_event",
            "ReservationEvent",
        ),
        (
            "backend/app/services/item_ledger/obligation_generation.py",
            "carry_forward_retained_reservations",
            "ReservationEvent",
        ),
    }


def test_direct_1c_writer_registry_is_exact() -> None:
    def post_export_entry_point(node: ast.Call) -> str | None:
        target = node.func
        if isinstance(target, ast.Name) and target.id in {
            "_post_export_entries",
            "post_export_entries",
        }:
            return "_post_export_entries"
        return None

    def direct_client_write(node: ast.Call) -> str | None:
        target = node.func
        if not isinstance(target, ast.Attribute):
            return None
        if target.attr not in {"post", "patch", "delete"}:
            return None
        owner = ast.unparse(target.value)
        if owner not in {"client", "self.client"}:
            return None
        return target.attr

    actual: set[tuple[str, str, str]] = set()
    for path in _python_sources(REPO / "backend/app/services"):
        actual |= _enclosing_call_sites(path, direct_client_write)
        actual |= _enclosing_call_sites(path, post_export_entry_point)

    # Specification writeback is the sole catalog writer; document creation
    # and mutation remain inside the sanctioned one_c export owners.
    assert actual == {
        (
            "backend/app/services/one_c_export_common.py",
            "post_export_entries",
            "post",
        ),
        (
            "backend/app/services/one_c_purchase_order_export.py",
            "create_purchase_order_document",
            "post",
        ),
        (
            "backend/app/services/one_c_purchase_order_export.py",
            "export_planned_purchases_to_1c",
            "patch",
        ),
        (
            "backend/app/services/one_c_production_order_export.py",
            "export_production_orders_to_1c",
            "_post_export_entries",
        ),
        (
            "backend/app/services/one_c_stock_transfer_export.py",
            "export_material_issues_to_1c",
            "_post_export_entries",
        ),
        (
            "backend/app/services/one_c_manufacture_export.py",
            "export_manufactures_to_1c",
            "_post_export_entries",
        ),
        (
            "backend/app/services/one_c_piecework_export.py",
            "export_piecework_to_1c",
            "_post_export_entries",
        ),
        (
            "backend/app/services/one_c_piecework_export.py",
            "export_chain_piecework_to_1c",
            "_post_export_entries",
        ),
        (
            "backend/app/services/spec_writeback_1c.py",
            "patch_sostav",
            "patch",
        ),
    }


def test_replenishment_formula_owner_is_unique() -> None:
    owner = "backend/app/services/item_ledger/reservation.py"
    definitions: set[tuple[str, str]] = set()
    suspicious_subtractions: set[tuple[str, int]] = set()

    for path in _python_sources(REPO / "backend/app"):
        relative = path.relative_to(REPO).as_posix()
        tree = ast.parse(_read(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in {
                    "freeze_reservation_amounts",
                    "replenishment_remaining",
                    "replenishment_execution_pct",
                }:
                    definitions.add((relative, node.name))
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
                expression = ast.unparse(node).casefold()
                if (
                    "required" in expression
                    and any(
                        token in expression
                        for token in ("received", "fulfilled", "realized")
                    )
                    and relative != owner
                ):
                    suspicious_subtractions.add((relative, node.lineno))

    assert definitions == {
        (owner, "freeze_reservation_amounts"),
        (owner, "replenishment_remaining"),
        (owner, "replenishment_execution_pct"),
    }
    assert not suspicious_subtractions


def test_assembly_queue_calculator_owner_is_unique() -> None:
    owners: set[tuple[str, str]] = set()
    for path in _python_sources(REPO / "backend/app"):
        relative = path.relative_to(REPO).as_posix()
        tree = ast.parse(_read(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name == "build_assembly_queue_snapshot"
            ):
                owners.add((relative, node.name))

    assert owners == {
        (
            "backend/app/services/item_ledger/assembly_queue_snapshot.py",
            "build_assembly_queue_snapshot",
        )
    }


def test_active_python_has_no_ghost_design_references() -> None:
    forbidden = (
        "design §",
        "design Прил.",
        "Inc0",
        "Inc1",
        "Inc2",
        "Inc3",
        "Inc4",
        "Inc5",
        "Inc6",
        "Increment ",
    )
    offenders = {
        path.relative_to(REPO).as_posix(): [
            token for token in forbidden if token in _read(path)
        ]
        for root in (REPO / "backend/app", REPO / "tests")
        for path in _python_sources(root)
        if path != Path(__file__).resolve()
        if any(token in _read(path) for token in forbidden)
    }
    assert not offenders


def test_canon_invariants_are_an_explicit_ci_gate() -> None:
    workflow = _read(REPO / ".github/workflows/canon.yml")
    assert "pytest -q tests/test_canon_invariants.py" in workflow
    assert "needs: canon-invariants" in workflow
