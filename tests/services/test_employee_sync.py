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


def _brigades_request(dry_run=False):
    return ODataSyncRequest(
        base_url="http://mtzw7/unf_demo/odata/standard.odata",
        entity_name="Catalog_Бригады",
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
    assert [row.employee_type for row in rows] == ["employee", "employee"]
    assert rows[1].deletion_mark is True


def test_sync_brigades_creates_brigade_executor_rows(db_session, monkeypatch):
    _FakeODataClient.count = 1
    _FakeODataClient.pages = [
        [
            {
                "Ref_Key": "33333333-3333-3333-3333-333333333333",
                "Code": "000000022",
                "Description": "Сварщики с 01.06.2026",
                "DeletionMark": False,
                "DataVersion": "AAAAAw",
            },
        ]
    ]
    monkeypatch.setattr(employee_sync, "OData1CClient", _FakeODataClient)

    stats = sync_employees_from_odata(db_session, _brigades_request())

    assert stats["employees_created"] == 1
    row = db_session.query(Employee).one()
    assert row.employee_name == "Сварщики с 01.06.2026"
    assert row.employee_type == "brigade"


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


def test_sync_employees_keeps_local_name_when_1c_omits_description(
    db_session, monkeypatch
):
    """Regression: a Description-less answer used to rename the row to its GUID."""
    db_session.add(
        Employee(
            employee_ref1c="11111111-1111-1111-1111-111111111111",
            employee_code="0001",
            employee_name="Иванов Иван",
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
                "DataVersion": "new",
            }
        ]
    ]
    monkeypatch.setattr(employee_sync, "OData1CClient", _FakeODataClient)

    sync_employees_from_odata(db_session, _request())

    employee = db_session.query(Employee).one()
    assert employee.employee_name == "Иванов Иван"


def test_sync_employees_uses_ref_as_name_only_for_new_rows(db_session, monkeypatch):
    _FakeODataClient.count = 1
    _FakeODataClient.pages = [
        [{"Ref_Key": "22222222-2222-2222-2222-222222222222"}]
    ]
    monkeypatch.setattr(employee_sync, "OData1CClient", _FakeODataClient)

    sync_employees_from_odata(db_session, _request())

    employee = db_session.query(Employee).one()
    assert employee.employee_name == "22222222-2222-2222-2222-222222222222"


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
