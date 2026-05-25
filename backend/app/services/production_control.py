from __future__ import annotations

# Compatibility facade for older imports. New code should import from the
# focused modules below directly.
from .production_control_journal import create_orders_from_mrp, list_journal, update_line_state
from .production_control_material_availability import _components_for_product, preview_materials
from .production_control_material_issues import (
    build_issue_1c_payload,
    create_material_issues,
    export_issue_to_1c,
    get_issue,
)
from .production_control_printing import mark_route_sheets_printed, render_route_sheets_html
from .production_control_production_flow import produce_line, return_leftover_components
from .production_control_settings import (
    delete_ignored_warehouse,
    delete_workshop_binding,
    list_settings,
    upsert_ignored_warehouse,
    upsert_workshop_binding,
)

__all__ = [
    "_components_for_product",
    "build_issue_1c_payload",
    "create_material_issues",
    "create_orders_from_mrp",
    "delete_ignored_warehouse",
    "delete_workshop_binding",
    "export_issue_to_1c",
    "get_issue",
    "list_journal",
    "list_settings",
    "mark_route_sheets_printed",
    "preview_materials",
    "produce_line",
    "render_route_sheets_html",
    "return_leftover_components",
    "update_line_state",
    "upsert_ignored_warehouse",
    "upsert_workshop_binding",
]
