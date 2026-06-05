from __future__ import annotations

from app.models import Operation
from app.schemas import ODataSyncRequest
from app.services import operations_sync


class _FakeODataClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_count(self, *_args, **_kwargs):
        return 1

    def iter_pages(self, *_args, **_kwargs):
        yield [
            {
                "Ref_Key": "spec-row-1",
                "Операция_Key": "op-ref-price",
                "НормаВремени": 0.2,
                "Операция@navigationLinkUrl": "Catalog_Номенклатура(guid'op-ref-price')",
            }
        ]

    def _make_request(self, *_args, **_kwargs):
        return {
            "Ref_Key": "op-ref-price",
            "Code": "00000223",
            "Description": "Корпус подш ведущ вала, сборка/сварка",
            "НормаВремени": 0.4,
            "Цены": [
                {
                    "ВидЦен": {"Description": "Розничная цена"},
                    "Цена": 99,
                },
                {
                    "ВидЦен": {"Description": "Учетная цена"},
                    "Цена": 30,
                },
            ],
        }


def test_operations_sync_imports_operation_price(db_session, monkeypatch):
    monkeypatch.setattr(operations_sync, "OData1CClient", _FakeODataClient)

    result = operations_sync.sync_operations_from_odata(
        db_session,
        ODataSyncRequest(
            base_url="http://example.test/odata",
            username="u",
            password="p",
            entity_name="Catalog_Спецификации_Операции",
            dry_run=False,
        ),
    )

    assert result["operations_created"] == 1
    operation = db_session.query(Operation).filter_by(operation_ref1c="op-ref-price").one()
    assert operation.operation_name == "Корпус подш ведущ вала, сборка/сварка"
    assert float(operation.time_norm) == 0.4
    assert float(operation.operation_price) == 30.0
