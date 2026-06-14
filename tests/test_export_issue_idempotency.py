"""M-4 (residual): the legacy export_issue_to_1c must not re-POST a document
that already has a 1C ref — a repeat would create a duplicate stock transfer.
"""

from app.models import ProductionMaterialIssue
from app.schemas import ODataSyncRequest
from app.services import odata_client as odata_client_module
from app.services.production_control_material_issues import export_issue_to_1c


def test_already_exported_issue_is_not_reposted(db_session, monkeypatch):
    db = db_session
    issue = ProductionMaterialIssue(
        document_number="MI-DUP-1",
        product_id=1,
        order_id=1,
        status="exported",
        exported_ref1c="ref-already-in-1c",
    )
    db.add(issue)
    db.flush()

    # Any attempt to talk to 1C for an already-exported issue is a bug.
    def boom(*args, **kwargs):
        raise AssertionError("OData1CClient must not be used for an already-exported issue")

    monkeypatch.setattr(odata_client_module, "OData1CClient", boom)

    req = ODataSyncRequest(
        base_url="http://unused",
        entity_name="Document_ПеремещениеЗапасов",
        dry_run=False,
    )
    result = export_issue_to_1c(db, issue.issue_id, req)

    assert result["status"] == "already_exported"
    assert result["exported_ref1c"] == "ref-already-in-1c"
