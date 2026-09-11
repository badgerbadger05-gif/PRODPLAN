"""Public MRP result anchor is a CurrentExecutionScope id, not a snapshot id."""

import inspect

from app.routers import plan as plan_router
from app.services import mrp_result_projection


def test_mrp_service_uses_current_scope_id_anchor():
    assert "current_scope_id" in inspect.signature(
        mrp_result_projection.read_mrp_result_manifest
    ).parameters
    assert "current_scope_id" in inspect.signature(
        mrp_result_projection.read_mrp_result_rows
    ).parameters


def test_mrp_router_does_not_expose_snapshot_id_query_anchor():
    source = inspect.getsource(plan_router)
    assert "snapshot_id: Optional[int]" not in source
    assert "current_scope_id: Optional[int]" in source
