from __future__ import annotations

from app.services import one_c_order_completion_repair as repair


class _FakeClient:
    rows: list[dict] = []
    patches: list[tuple[str, dict]] = []

    def __init__(self, **_):
        pass

    def get_all(self, entity_name, **kwargs):
        self.__class__.last_get = (entity_name, kwargs)
        return list(self.__class__.rows)

    def patch(self, entity_ref, payload, **_):
        self.__class__.patches.append((entity_ref, payload))
        return {}


def test_repair_completion_success_filters_pp_and_patches_missing_flag(monkeypatch):
    _FakeClient.rows = [
        {
            "Ref_Key": "ref-pp-missing",
            "Number": "PP001509913",
            "СостояниеЗаказа_Key": repair.DONE_STATE_KEY,
            "ВариантЗавершения": "",
        },
        {
            "Ref_Key": "ref-pp-ok",
            "Number": "PP001410223",
            "СостояниеЗаказа_Key": repair.DONE_STATE_KEY,
            "ВариантЗавершения": repair.ORDER_COMPLETION_SUCCESS,
        },
        {
            "Ref_Key": "ref-manual",
            "Number": "ЗСНФ-002096",
            "СостояниеЗаказа_Key": repair.DONE_STATE_KEY,
            "ВариантЗавершения": "",
        },
        {
            "Ref_Key": "ref-pp-cancelled",
            "Number": "PP001509506",
            "СостояниеЗаказа_Key": repair.DONE_STATE_KEY,
            "ВариантЗавершения": "Отменен",
        },
    ]
    _FakeClient.patches = []
    monkeypatch.setattr(
        repair,
        "_load_odata_config",
        lambda: {"base_url": "http://demo/odata/unf", "username": "u", "password": "p"},
    )
    monkeypatch.setattr(repair, "OData1CClient", _FakeClient)

    result = repair.repair_prodplan_order_completion_success(
        dry_run=False,
        allow_production=True,
        number_prefix="PP",
    )

    assert result["rows_loaded"] == 4
    assert result["skipped_by_number"] == 1
    assert result["candidates"] == 3
    assert result["already_ok"] == 1
    assert result["skipped_non_empty"] == 1
    assert result["patched"] == 1
    assert _FakeClient.patches == [
        (
            "Document_ЗаказНаПроизводство(guid'ref-pp-missing')",
            {"ВариантЗавершения": repair.ORDER_COMPLETION_SUCCESS},
        )
    ]


def test_repair_completion_success_dry_run_does_not_patch(monkeypatch):
    _FakeClient.rows = [
        {
            "Ref_Key": "ref-pp-missing",
            "Number": "PP001509913",
            "СостояниеЗаказа_Key": repair.DONE_STATE_KEY,
            "ВариантЗавершения": None,
        }
    ]
    _FakeClient.patches = []
    monkeypatch.setattr(repair, "_load_odata_config", lambda: {"base_url": "http://demo"})
    monkeypatch.setattr(repair, "OData1CClient", _FakeClient)

    result = repair.repair_prodplan_order_completion_success(dry_run=True)

    assert result["patched"] == 0
    assert result["rows"][0]["status"] == "dry_run"
    assert _FakeClient.patches == []
