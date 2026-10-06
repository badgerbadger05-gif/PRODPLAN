from __future__ import annotations

import copy

import pytest

from app.services import spec_writeback_1c as wb


SPEC_REF = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OWNER_REF = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OLD_KIND = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TARGET_KIND = "dddddddd-dddd-dddd-dddd-dddddddddddd"
SHEET_OPERATION = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def _spec(**overrides):
    record = {
        "Ref_Key": SPEC_REF,
        "Code": "NF-001",
        "Description": "Laser sheet part",
        "Owner_Key": OWNER_REF,
        "DeletionMark": False,
        "Недействителен": False,
        "ЭтоШаблон": False,
        "DataVersion": "VERSION-1",
        "ВидПроизводства_Key": OLD_KIND,
        "Состав": [{"LineNumber": "1", "Количество": 1, "Unknown": "keep"}],
        "Операции": [
            {"LineNumber": "1", "Операция_Key": SHEET_OPERATION, "НормаВремени": 0.002}
        ],
        "AdditionalUnknownField": {"nested": [1, 2, 3]},
    }
    record.update(overrides)
    return record


class _FakeClient:
    def __init__(self, record, *, apply_patch=True, mutate_extra=False):
        self.record = copy.deepcopy(record)
        self.apply_patch = apply_patch
        self.mutate_extra = mutate_extra
        self.get_calls = []
        self.patches = []

    def get_all(self, entity, filter_query=None, select_fields=None):
        self.get_calls.append((entity, filter_query, select_fields))
        return [copy.deepcopy(self.record)]

    def patch(self, endpoint, payload):
        self.patches.append((endpoint, copy.deepcopy(payload)))
        if self.apply_patch:
            self.record.update(copy.deepcopy(payload))
            self.record["DataVersion"] = "VERSION-2"
        if self.mutate_extra:
            self.record["Description"] = "unexpected mutation"
        return {"ok": True}


def _call(client, **overrides):
    kwargs = {
        "spec_ref": SPEC_REF,
        "expected_owner_ref": OWNER_REF,
        "expected_code": "NF-001",
        "old_kind_ref": OLD_KIND,
        "target_kind_ref": TARGET_KIND,
        "expected_operation_ref": SHEET_OPERATION,
    }
    kwargs.update(overrides)
    return wb.writeback_change_production_kind(client, **kwargs)


def test_kind_change_dry_run_reads_full_object_and_does_not_patch():
    client = _FakeClient(_spec())

    result = _call(client)

    assert result["status"] == "dry_run"
    assert result["dry_run"] is True
    assert result["would_patch"] == {"ВидПроизводства_Key": TARGET_KIND}
    assert client.patches == []
    assert client.get_calls == [
        (
            "Catalog_Спецификации",
            f"Ref_Key eq guid'{SPEC_REF}'",
            None,
        )
    ]


def test_kind_change_patches_one_header_field_and_verifies_full_readback():
    before = _spec()
    client = _FakeClient(before)

    result = _call(client, dry_run=False, expected_data_version="VERSION-1")

    assert result["status"] == "updated"
    assert result["after_data_version"] == "VERSION-2"
    assert client.patches == [
        (
            f"Catalog_Спецификации(guid'{SPEC_REF}')",
            {"ВидПроизводства_Key": TARGET_KIND},
        )
    ]
    assert len(client.get_calls) == 2
    expected = copy.deepcopy(before)
    expected["ВидПроизводства_Key"] = TARGET_KIND
    expected["DataVersion"] = "VERSION-2"
    assert client.record == expected


def test_kind_change_is_resumable_when_target_kind_is_already_set():
    client = _FakeClient(_spec(**{"ВидПроизводства_Key": TARGET_KIND, "DataVersion": "VERSION-9"}))

    result = _call(client, dry_run=False, expected_data_version="VERSION-1")

    assert result["status"] == "already_applied"
    assert client.patches == []


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_spec(Ref_Key="wrong"), "Ref_Key"),
        (_spec(Owner_Key="wrong"), "Owner_Key"),
        (_spec(Code="wrong"), "Code"),
        (_spec(DeletionMark=True), "DeletionMark"),
        (_spec(**{"ВидПроизводства_Key": "third-kind"}), "ВидПроизводства_Key"),
        (_spec(**{"Операции": []}), "ровно 1 операция"),
        (
            _spec(**{"Операции": [{"Операция_Key": "wrong"}]}),
            "Операция_Key",
        ),
    ],
)
def test_kind_change_rejects_source_drift_before_patch(record, message):
    client = _FakeClient(record)

    with pytest.raises(wb.SpecWritebackError, match=message):
        _call(client, dry_run=False)

    assert client.patches == []


def test_kind_change_rejects_data_version_drift_before_patch():
    client = _FakeClient(_spec())

    with pytest.raises(wb.SpecWritebackError, match="DataVersion"):
        _call(client, dry_run=False, expected_data_version="STALE")

    assert client.patches == []


def test_kind_change_rejects_empty_or_same_route_before_read():
    client = _FakeClient(_spec())

    with pytest.raises(wb.SpecWritebackError, match="пустые обязательные поля"):
        _call(client, spec_ref="")
    with pytest.raises(wb.SpecWritebackError, match="совпадают"):
        _call(client, target_kind_ref=OLD_KIND)

    assert client.get_calls == []
    assert client.patches == []


def test_kind_change_rejects_missing_or_ambiguous_full_get():
    class _BadClient:
        def __init__(self, rows):
            self.rows = rows

        def get_all(self, *_args, **_kwargs):
            return self.rows

    for rows in ([], [_spec(), _spec()]):
        with pytest.raises(wb.SpecWritebackError, match="ровно 1 спецификация"):
            _call(_BadClient(rows))


def test_kind_change_rejects_readback_without_target_kind():
    client = _FakeClient(_spec(), apply_patch=False)

    with pytest.raises(wb.SpecWritebackError, match="read-back"):
        _call(client, dry_run=False)


def test_kind_change_rejects_any_unintended_readback_change():
    client = _FakeClient(_spec(), mutate_extra=True)

    with pytest.raises(wb.SpecWritebackError, match="read-back"):
        _call(client, dry_run=False)

