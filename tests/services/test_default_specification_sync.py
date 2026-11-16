from app import models
from app.schemas import ODataSyncRequest
from app.services.default_specification_sync import sync_default_specifications_from_odata
from app.services.odata_client import OData1CClient


def test_default_specification_sync_reports_and_recovers_missing_dependencies(db_session, monkeypatch):
    item_ref = "d54dfbe4-e1a4-11f1-8ede-9ee51454587f"
    spec_ref = "d592a42e-e1a4-11f1-8ede-9ee51454587f"
    register = "InformationRegister_СпецификацииПоУмолчанию"
    monkeypatch.setattr(
        OData1CClient,
        "get_all",
        lambda *_args, **_kwargs: [{
            "Номенклатура_Key": item_ref,
            "Характеристика_Key": "00000000-0000-0000-0000-000000000000",
            "Спецификация_Key": spec_ref,
        }],
    )
    req = ODataSyncRequest(base_url="http://example.invalid/odata", entity_name=register, dry_run=True)
    item = models.Item(item_code="НФ-00012530", item_ref1c=item_ref, item_name="EGER22")
    db_session.add(item)
    db_session.commit()

    missing = sync_default_specifications_from_odata(db_session, req)
    assert missing["records_created"] == 0
    assert missing["records_missing_item"] == 0
    assert missing["records_missing_specification"] == 1

    spec = models.Specification(spec_ref1c=spec_ref, spec_name="EGER22")
    db_session.add(spec)
    db_session.commit()
    req.dry_run = False
    retry = sync_default_specifications_from_odata(db_session, req)
    assert retry["records_created"] == 1
    assert retry["records_missing_specification"] == 0
    row = db_session.query(models.DefaultSpecification).one()
    assert (row.item_id, row.spec_id, row.characteristic_id) == (item.item_id, spec.spec_id, None)

    repeat = sync_default_specifications_from_odata(db_session, req)
    assert repeat["records_created"] == 0
    assert repeat["records_unchanged"] == 1
