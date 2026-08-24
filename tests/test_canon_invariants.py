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


def _read_owner_document(name: str) -> str:
    """Read an owner-decision document from its canonical home in the repo.

    Per .docs/CANON.md (owner decision 2026-07-30) these live in .docs/notes/
    inside the repository; workstation copies are secondary and
    machine-specific absolute paths are forbidden. A missing document is a
    hard failure, never a silent skip.
    """
    path = REPO / ".docs/notes" / name
    assert path.is_file(), f"owner document missing from .docs/notes/: {name}"
    return _read(path)


# Рабочий каталог держит внутри репозитория посторонние чекауты: worktree фоновых
# задач и распакованные копии для деплоя. Это не исходники проекта, и канон по
# ним не проверяется — иначе сторож ловит копию самого себя.
_SCRATCH_DIRS = {".tmp", ".claude", ".git", "node_modules", "__pycache__", ".venv"}


def _python_sources(root: Path):
    return sorted(
        path
        for path in root.rglob("*.py")
        if path.is_file()
        and not _SCRATCH_DIRS.intersection(path.relative_to(root).parts)
    )


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
        "backend/app/services/item_ledger/historical_replay_core.py",
        "backend/app/services/item_ledger/historical_replay_persistence.py",
        "backend/app/services/item_ledger/supplier_receipt_allocation.py",
        "backend/app/services/production_output_truth.py",
        "backend/app/services/bom_specification_resolver.py",
        "backend/app/services/mrp_freeze.py",
        "backend/app/services/planning_service.py",
        "backend/app/services/item_ledger/generation_lifecycle.py",
        "backend/app/services/obligation_refresh_orchestrator.py",
        "backend/app/services/planning_truth.py",
    )

    missing = [path for path in required_paths if not (REPO / path).is_file()]
    assert not missing, f"CANON points to missing modules: {missing}"


def test_execution_contract_uses_percent_and_caps_execution() -> None:
    decisions = _read_owner_document("mrp-decisions-log.md")
    reservation = _read(REPO / ".docs/reservation-replenishment-core.md")

    for document in (decisions, reservation):
        assert "replenishment_required_qty * 100" in document
        assert "min(" in document
        assert "replenishment_received_qty" in document
    assert "0 <= execution_pct <= 100" in reservation
    assert "от 0 до 100" in decisions


def test_owner_decision_keeps_address_first_fifo_policy() -> None:
    decisions = _read_owner_document("mrp-decisions-log.md")
    canon = _read(REPO / ".docs/CANON.md")
    truth = _read(REPO / ".docs/planning-truth-contract.md")
    reservation = _read(REPO / ".docs/reservation-replenishment-core.md")

    required = (
        "точная канонически подтверждённая связь",
        "излишек",
        "неизвестному PRODPLAN заказу 1С",
        "неполная, обрезанная или отфильтрованная выгрузка",
    )
    for token in required:
        assert token.casefold() in decisions.casefold()

    for document in (canon, truth, reservation):
        assert "адрес" in document.casefold()
        assert "FIFO" in document

    retired_phrases = (
        "Requirement/order identity is provenance only",
        "Ссылка на закупочный или производственный заказ является только provenance",
        "при открытых заказах с обеих сторон ничего не гасит",
        "Правило назначения всегда FIFO",
    )
    active_documents = (decisions, canon, truth, reservation)
    found = [
        phrase
        for phrase in retired_phrases
        if any(phrase in document for document in active_documents)
    ]
    assert not found, f"Retired FIFO-only policy returned: {found}"


def test_python_sources_do_not_keep_retired_historical_replay_prose() -> None:
    retired_phrases = (
        "Requirement/order identity is provenance only",
        "Ссылка на закупочный или производственный заказ является только provenance",
        "при открытых заказах с обеих сторон ничего не гасит",
        "Правило назначения всегда FIFO",
    )
    current_test_file = REPO / "tests/test_canon_invariants.py"
    offenders = [
        str(path.relative_to(PRODPLAN))
        for path in _python_sources(REPO)
        if path != current_test_file
        if any(phrase in _read(path) for phrase in retired_phrases)
    ]
    assert not offenders, f"Retired replay prose found in python sources: {offenders}"


def test_owner_decisions_define_supplier_s0_and_master_data_edges() -> None:
    decisions = _read_owner_document("mrp-decisions-log.md")
    canon = _read(REPO / ".docs/CANON.md")
    reservation = _read(REPO / ".docs/reservation-replenishment-core.md")
    truth = _read(REPO / ".docs/planning-truth-contract.md")
    shelves = _read(REPO / ".docs/shelves-buffers-and-mechshop-pull.md")

    decision_tokens = (
        "PurchaseExportObligationAllocation",
        "allocated_qty",
        "newest-first",
        "senior_hold_qty",
        "accepted_attributed_consumption_qty",
        "Item.replenishment_time",
        "Значение `0` валидно",
        "не считается\nпроизводством по умолчанию",
    )
    for token in decision_tokens:
        assert token in decisions

    for document in (canon, reservation, truth):
        assert "allocated_qty" in document
        assert "newest-first" in document
        assert "unavailable" in document

    assert "free_s0_qty" in reservation
    assert "Поступление удержание не освобождает" in reservation
    assert "ShelfPolicy.replenishment_time_days" in canon
    assert "Item.replenishment_time" in canon
    assert "Ни одно из этих полей не подменяет другое" in shelves

    one_c = _read(REPO / ".docs/one_c_export_from_prodplan.md")
    assert "Как обрабатывать отмены, сторно и возвраты поставщику" not in one_c


def test_plan_output_field_name_is_canonical() -> None:
    design = _read_owner_document("mrp-item-ledger-design.md")
    assembly = _read(REPO / ".docs/assembly-queue-and-drum.md")

    for document in (assembly, design):
        assert "accepted_plan_output_qty" in document
        assert "accepted_output_qty" not in document


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

    forbidden_mutation_routes = (
        '/orders/dedupe-mrp',
        'update_product_quantity',
        'dedupe_mrp_production_orders',
    )
    returned = [
        token for token in forbidden_mutation_routes if token in production_router
    ]
    assert not returned, (
        f"non-canonical quantity mutation returned to production router: {returned}"
    )

    # Правка количества к запуску запрещена не как таковая, а как бесконтрольная.
    # Выведенный `update_product_quantity` переписывал количество любой строки в
    # любой момент — включая уже открытую в 1С и уже выпущенную. Сторож поэтому
    # проверяет не имя маршрута, а то, что у правки есть все ограничения: пока
    # заказ не стал фактом снаружи и не выше незакрытой потребности.
    journal_service = _read(REPO / "backend/app/services/production_control_journal.py")
    if '/orders/{product_id}/quantity' in production_router:
        assert 'def update_local_order_quantity(' in journal_service, (
            "маршрут правки количества есть, а канонической службы под ним нет"
        )
        required_guards = (
            '_production_order_has_1c_link(db, order)',
            'accepted_product_output(product)',
            'PaintWeldChainLink',
            '_material_issue_has_1c_link(db, issue)',
            'launch_allowance_for_product(',
            'require_accepted_truth(',
        )
        unguarded = [
            token for token in required_guards if token not in journal_service
        ]
        assert not unguarded, (
            f"правка количества к запуску вернулась без ограничений: {unguarded}"
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

    frontend_sources = "\n".join(
        _read(path)
        for root in (
            REPO / "frontend-erp-shell/src/services",
            REPO / "frontend-erp-shell/src/ui/pages",
        )
        for path in sorted(root.rglob("*.ts*"))
        if not path.name.endswith((".test.ts", ".test.tsx"))
    )
    forbidden_dead_routes = (
        "/orders/from-mrp",
        "/orders/from-mrp-requirements",
        "/results/${runId}/reconcile",
    )
    returned = [token for token in forbidden_dead_routes if token in frontend_sources]
    assert not returned, f"frontend calls removed mutation routes: {returned}"


def test_stage_distribution_and_distribution_calculator_contour_is_removed() -> None:
    main = _read(REPO / "backend/app/main.py")
    resources_router = _read(REPO / "backend/app/routers/resources.py")
    forbidden_backend_tokens = (
        "calculate_distribution",
        "calculate_resource_distribution",
        "calculate_stages",
        "get_resource_distribution_api_v1_resources_calculate_distribution_post",
        "router.post(\"/calculate\"",
        "/v1/stages/calculate",
    )
    returned = [token for token in forbidden_backend_tokens if token in resources_router or token in main]
    assert not returned, f"legacy distribution contour still referenced in backend routers/main: {returned}"

    assert not (REPO / "backend/app/services/resource_calculator.py").exists(), "legacy resource calculator module still exists"
    assert not (REPO / "backend/app/services/stages_calculator.py").exists(), "legacy stages calculator module still exists"
    assert not (REPO / "backend/app/routers/stages.py").exists(), "legacy stages calculation route still exists"

    ui_app = _read(REPO / "frontend-erp-shell/src/ui/App.tsx")
    ui_registry = _read(REPO / "frontend-erp-shell/src/ui/resourceRegistry.ts")
    ui_home = _read(REPO / "frontend-erp-shell/src/ui/pages/HomePage.tsx")
    ui_frontend_forbidden = (
        "/stage-distribution",
        "StageDistribution",
        "stage_distribution",
        "Распределение этапов",
    )
    ui_returned = [token for token in ui_frontend_forbidden if token in ui_app or token in ui_registry or token in ui_home]
    assert not ui_returned, f"Stage Distribution UI fragment still present: {ui_returned}"

    removed_frontend_paths = (
        REPO / "frontend-erp-shell/src/services/stageDistribution.ts",
        REPO / "frontend-erp-shell/src/domain/stageDistribution.ts",
        REPO / "frontend-erp-shell/src/ui/pages/StageDistributionPage.tsx",
        REPO / "frontend-erp-shell/tests/smoke/stage-distribution-visual.spec.ts",
    )
    existing_paths = [str(path.relative_to(PRODPLAN)) for path in removed_frontend_paths if path.exists()]
    assert not existing_paths, f"legacy Stage Distribution frontend modules still exist: {existing_paths}"


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
        if target.attr not in {"post", "put", "patch", "delete"}:
            return None
        owner = ast.unparse(target.value)
        if owner not in {"client", "self.client"}:
            return None
        return target.attr

    def aliased_client_write_sites(path: Path) -> set[tuple[str, str, str]]:
        tree = ast.parse(_read(path), filename=str(path))
        relative = path.relative_to(REPO).as_posix()
        sites: set[tuple[str, str, str]] = set()

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.functions: list[str] = []
                self.aliases: list[dict[str, str]] = []

            def visit_FunctionDef(self, node):
                self.functions.append(node.name)
                self.aliases.append({})
                self.generic_visit(node)
                self.aliases.pop()
                self.functions.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Assign(self, node):
                value = node.value
                if (
                    self.aliases
                    and isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "getattr"
                    and len(value.args) >= 2
                    and ast.unparse(value.args[0]) in {"client", "self.client"}
                    and isinstance(value.args[1], ast.Constant)
                    and value.args[1].value in {"post", "put", "patch", "delete", "post_operation"}
                ):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.aliases[-1][target.id] = str(value.args[1].value)
                self.generic_visit(node)

            def visit_Call(self, node):
                if (
                    self.aliases
                    and isinstance(node.func, ast.Name)
                    and node.func.id in self.aliases[-1]
                ):
                    sites.add(
                        (
                            relative,
                            self.functions[-1],
                            self.aliases[-1][node.func.id],
                        )
                    )
                self.generic_visit(node)

        Visitor().visit(tree)
        return sites

    actual: set[tuple[str, str, str]] = set()
    for path in _python_sources(REPO / "backend/app/services"):
        actual |= _enclosing_call_sites(path, direct_client_write)
        actual |= _enclosing_call_sites(path, post_export_entry_point)
        actual |= aliased_client_write_sites(path)

    # Specification writeback is the sole catalog writer; document creation
    # and mutation remain inside the sanctioned one_c export owners.
    assert actual == {
        (
            "backend/app/services/one_c_export_common.py",
            "post_export_entries",
            "post",
        ),
        (
            "backend/app/services/one_c_export_common.py",
            "post_document_operational",
            "post_operation",
        ),
        (
            "backend/app/services/one_c_export_common.py",
            "post_export_entries",
            "post_operation",
        ),
        (
            "backend/app/services/one_c_export_common.py",
            "post_export_entries",
            "patch",
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
            "backend/app/services/one_c_production_order_export.py",
            "close_production_orders_to_1c",
            "patch",
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
            "backend/app/services/one_c_piecework_export.py",
            "_mark_success",
            "patch",
        ),
        (
            "backend/app/services/production_control_material_issues.py",
            "assemble_material_issue",
            "patch",
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
                    "replenishment_execution_status",
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
        (owner, "replenishment_execution_status"),
    }
    assert not suspicious_subtractions

    period_plan = _read(REPO / "backend/app/services/period_plan_service.py")
    assert "def _rounded_replenishment_pct" in period_plan
    assert "completed_qty / progress_base_qty" not in period_plan
    assert "execution_completed_qty / execution_base_qty" not in period_plan
    assert "execution_completed_qty / execution_total_base_qty" not in period_plan
    period_plan_ui = _read(
        REPO / "frontend-erp-shell/src/ui/pages/period-plan/PeriodPlanListView.tsx"
    )
    assert "pct >= 100" not in period_plan_ui
    assert "execution_progress_status" in period_plan_ui


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


def test_item_level_physical_stock_cache_is_removed() -> None:
    models = _read(REPO / "backend/app/models.py")
    schemas = _read(REPO / "backend/app/schemas.py")
    active_roots = (
        REPO / "backend/app",
        REPO / "frontend-erp-shell/src",
    )
    forbidden = (
        "Item.stock_qty",
        "items.stock_qty",
        "item.stock_qty",
        "it.stock_qty",
    )
    offenders = []
    for root in active_roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or not path.is_file():
                continue
            text = _read(path)
            if any(token in text for token in forbidden):
                offenders.append(path.relative_to(REPO).as_posix())

    item_class = models.split("class Item(Base):", 1)[1].split("class ItemCategory", 1)[0]
    item_schema = schemas.split("class ItemBase", 1)[1].split("class ItemCategoryBase", 1)[0]
    assert "stock_qty" not in item_class
    assert "stock_qty" not in item_schema
    assert not offenders, f"legacy Item stock cache references remain: {offenders}"


def test_route_sheet_live_fallback_renderer_is_removed() -> None:
    production_services = _read(REPO / "backend/app/services/production_control_printing.py")
    production_routes = _read(REPO / "backend/app/routers/production_control.py")
    assert "def render_route_sheets_html" not in production_services
    assert "render_route_sheets_html(" not in production_services
    assert (
        "render_route_sheets_from_snapshots(" in production_routes
        and "read_route_sheet_snapshot_rows(" in production_routes
    )


def test_production_control_journal_removes_legacy_capacity_scheduler_fallback() -> None:
    journal_source = _read(REPO / "backend/app/services/production_control_journal.py")
    fixation_source = _read(REPO / "backend/app/services/period_plan_service.py")
    assert "CapacityScheduler" not in journal_source
    assert "legacy CapacityScheduler" not in journal_source
    assert "def _planned_dates_by_item(" not in journal_source
    assert "CapacityScheduler" not in fixation_source
    assert "capacity_scheduler" not in fixation_source
    assert not (REPO / "backend/app/services/capacity_scheduler.py").exists()


def test_queue_period_and_shelf_transfer_decisions_have_single_saved_inputs() -> None:
    queue = _read(REPO / "backend/app/services/item_ledger/assembly_queue_snapshot.py")
    drum = _read(REPO / "backend/app/services/item_ledger/drum_schedule_persistence.py")
    output = _read(REPO / "backend/app/services/item_ledger/assembly_output_persistence.py")
    shelf = _read(REPO / "backend/app/services/item_ledger/shelf_projection_core.py")

    assert "def _effective_period_from" not in queue
    assert "def _effective_period_to" not in queue
    assert "def _frozen_run_period" in queue
    assert "PlanningRun.period_from" not in drum
    assert "ProductionPlanHeader.period_from" not in drum
    assert "PlanningRun.period_from" not in output
    assert "ProductionPlanHeader.period_from" not in output
    assert "saved_addressed_transfer_qty" in shelf
    assert "transfer = min(addressed_transfer, other)" in shelf


def test_mrp_stock_contour_has_one_owner() -> None:
    owner = _read(REPO / "backend/app/services/mrp_stock_helpers.py")
    freeze = _read(REPO / "backend/app/services/mrp_freeze.py")
    reservation = _read(REPO / "backend/app/services/item_ledger/reservation_ledger.py")

    assert "def planning_warehouse_scope" in owner
    assert "def planning_stock_by_item" in owner
    assert "def _mrp_warehouse_scope" not in freeze
    assert "def _apply_mrp_warehouse_scope" not in freeze
    assert "return planning_stock_by_item" in freeze
    assert "planning_stock_by_item(" in reservation


def test_retired_obligation_refresh_action_has_no_live_acceptor() -> None:
    candidate_owner = _read(
        REPO / "backend/app/services/planning_run_candidate.py"
    )
    assert "def create_candidate_run(" not in candidate_owner

    acceptors = (
        "backend/app/services/obligation_refresh_manifest.py",
        "backend/app/services/obligation_refresh_publish.py",
        "backend/app/services/mrp_result_snapshot.py",
        "backend/app/services/item_ledger/candidate_realization_replay.py",
    )
    for relative_path in acceptors:
        source = _read(REPO / relative_path)
        assert 'action == "refresh"' not in source
        assert 'action in {"refresh"' not in source
        assert '{"refresh", "add"}' not in source


def test_forecast_shift_classification_has_one_backend_owner() -> None:
    owner = _read(REPO / "backend/app/services/forecast.py")
    assert "CRITICAL_FORECAST_SHIFT_DAYS = 5" in owner
    for relative_path in (
        "backend/app/services/period_plan_service.py",
        "backend/app/services/production_control_journal.py",
        "backend/app/services/planning_service.py",
    ):
        source = _read(REPO / relative_path)
        assert "forecast_payload" in source
        assert "shift > 5" not in source

    frontend = REPO / "frontend-erp-shell/src"
    offenders = [
        path.relative_to(REPO).as_posix()
        for path in frontend.rglob("*")
        if path.suffix in {".ts", ".tsx"}
        and path.is_file()
        and "days > 5" in _read(path)
    ]
    assert not offenders, f"frontend forecast threshold copies remain: {offenders}"

    mrp_result_ui = _read(
        REPO / "frontend-erp-shell/src/ui/pages/MrpResultPage.tsx"
    )
    assert "overload_hours || 0" not in mrp_result_ui
    assert "capacity_status === 'overloaded'" in mrp_result_ui


def test_frontend_contracts_do_not_reopen_canonical_enums() -> None:
    production = _read(
        REPO / "frontend-erp-shell/src/domain/productionControl.ts"
    )
    purchase = _read(REPO / "frontend-erp-shell/src/domain/purchaseControl.ts")
    specification = _read(
        REPO / "frontend-erp-shell/src/domain/specification.ts"
    )
    production_router = _read(REPO / "backend/app/routers/production_control.py")

    assert (
        "export type EmployeeOption = ApiSchemas['ProductionEmployeeOptionResponse']"
        in production
    )
    assert "employee_type: 'employee' | 'brigade' | string" not in production
    assert "PurchaseFactStatus = 'available' | 'unavailable' | string" not in purchase
    assert "severity: 'error' | 'warning' | 'info' | string" not in specification
    assert 'employee_type: Literal["employee", "brigade"]' in production_router
    assert 'getattr(row, "employee_type"' not in production_router


def test_application_startup_never_creates_schema_outside_alembic():
    main_source = (REPO / "backend/app/main.py").read_text(encoding="utf-8")
    assert ".create_all(" not in main_source
    assert "Base.metadata" not in main_source
