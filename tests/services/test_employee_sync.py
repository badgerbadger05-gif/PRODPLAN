from app.models import Employee
from app.schemas import ODataSyncRequest
from app.services import employee_sync
from app.services.employee_sync import sync_employees_from_odata


class _FakeODataClient:
    pages = []
    count = 0

    def __init__(self, base_url, username=None, password=None, token=None):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.token = token

    def get_count(self, entity_name, filter_query=None):
        return self.count

    def iter_pages(
        self,
        entity_name,
        filter_query=None,
        select_fields=None,
        top=1000,
        max_pages=1000,
        order_by="Ref_Key",
    ):
        yield from self.pages


def _request(dry_run=False):
    return ODataSyncRequest(
        base_url="http://mtzw7/unf_demo/odata/standard.odata",
        entity_name="Catalog_Сотрудники",
        username="odata.user",
        password="secret",
        dry_run=dry_run,
    )


def test_sync_employees_creates_rows(db_session, monkeypatch):
    _FakeODataClient.count = 2
    _FakeODataClient.pages = [
        [
            {
                "Ref_Key": "11111111-1111-1111-1111-111111111111",
                "Code": "0001",
                "Description": "Иванов Иван",
                "DeletionMark": False,
                "DataVersion": "AAAAAQ",
            },
            {
                "Ref_Key": "22222222-2222-2222-2222-222222222222",
                "Code": "0002",
                "Description": "Петров Петр",
                "DeletionMark": True,
                "DataVersion": "AAAAAg",
            },
        ]
    ]
    monkeypatch.setattr(employee_sync, "OData1CClient", _FakeODataClient)

    stats = sync_employees_from_odata(db_session, _request())

    assert stats["employees_total"] == 2
    assert stats["employees_created"] == 2
    assert stats["employees_updated"] == 0

    rows = db_session.query(Employee).order_by(Employee.employee_code.asc()).all()
    assert [row.employee_name for row in rows] == ["Иванов Иван", "Петров Петр"]
    assert rows[1].deletion_mark is True


def test_sync_employees_updates_existing_row(db_session, monkeypatch):
    db_session.add(
        Employee(
            employee_ref1c="11111111-1111-1111-1111-111111111111",
            employee_code="0001",
            employee_name="Старое имя",
            deletion_mark=False,
            data_version="old",
        )
    )
    db_session.commit()

    _FakeODataClient.count = 1
    _FakeODataClient.pages = [
        [
            {
                "Ref_Key": "11111111-1111-1111-1111-111111111111",
                "Code": "0001",
                "Description": "Иванов Иван",
                "DeletionMark": True,
                "DataVersion": "new",
            }
        ]
    ]
    monkeypatch.setattr(employee_sync, "OData1CClient", _FakeODataClient)

    stats = sync_employees_from_odata(db_session, _request())

    assert stats["employees_created"] == 0
    assert stats["employees_updated"] == 1
    employee = db_session.query(Employee).one()
    assert employee.employee_name == "Иванов Иван"
    assert employee.deletion_mark is True
    assert employee.data_version == "new"


def test_sync_employees_dry_run_rolls_back(db_session, monkeypatch):
    _FakeODataClient.count = 1
    _FakeODataClient.pages = [
        [
            {
                "Ref_Key": "11111111-1111-1111-1111-111111111111",
                "Code": "0001",
                "Description": "Иванов Иван",
            }
        ]
    ]
    monkeypatch.setattr(employee_sync, "OData1CClient", _FakeODataClient)

    stats = sync_employees_from_odata(db_session, _request(dry_run=True))

    assert stats["employees_created"] == 1
    assert db_session.query(Employee).count() == 0
