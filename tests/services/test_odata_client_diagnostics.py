"""Silent-failure diagnostics in the OData client.

Truncated pagination, an unreachable warehouse catalog and a failed
nomenclature-code resolve all used to look exactly like "everything is fine,
there is just less data".
"""

import logging

import pytest

import app.services.odata_client as odata_client
from app.services.odata_client import OData1CClient, _resolve_warehouse_mapping


def _guid(n: int) -> str:
    return f"{n:08d}-0000-0000-0000-000000000000"


def test_get_all_flags_max_pages_truncation(monkeypatch, caplog):
    client = OData1CClient("http://1c/odata")

    def _page(self, endpoint, params=None, **_kwargs):
        base = int(params["$skip"])
        return {"value": [{"Ref_Key": _guid(base + i)} for i in range(params["$top"])]}

    monkeypatch.setattr(OData1CClient, "_make_request", _page)
    with caplog.at_level(logging.WARNING, logger=odata_client.__name__):
        rows = client.get_all("Catalog_Номенклатура", top=2, max_pages=3)

    assert len(rows) == 6                       # 3 full pages, more data waiting
    assert client.last_result_truncated is True
    assert "TRUNCATED" in caplog.text


def test_get_all_complete_selection_is_not_flagged(monkeypatch):
    client = OData1CClient("http://1c/odata")
    monkeypatch.setattr(
        OData1CClient,
        "_make_request",
        lambda self, endpoint, params=None, **_k: {"value": [{"Ref_Key": _guid(1)}]},
    )
    rows = client.get_all("Catalog_Номенклатура", top=2, max_pages=3)
    assert len(rows) == 1
    assert client.last_result_truncated is False


def test_iter_by_guid_flags_truncation(monkeypatch, caplog):
    client = OData1CClient("http://1c/odata")
    seq = {"n": 0}

    def _page(self, endpoint, params=None, **_kwargs):
        seq["n"] += 1
        return {"value": [{"Ref_Key": _guid(seq["n"] * 10 + i)} for i in range(2)]}

    monkeypatch.setattr(OData1CClient, "_make_request", _page)
    with caplog.at_level(logging.WARNING, logger=odata_client.__name__):
        pages = list(client.iter_by_guid("Catalog_Номенклатура", top=2, max_pages=2))

    assert len(pages) == 2
    assert client.last_result_truncated is True
    assert "TRUNCATED" in caplog.text


def test_warehouse_mapping_collects_catalog_errors(monkeypatch, caplog):
    """Every candidate catalog failing is an outage, not "no such warehouse"."""
    client = OData1CClient("http://1c/odata")

    def _boom(self, endpoint, params=None, **_kwargs):
        raise RuntimeError(f"503 from {endpoint}")

    monkeypatch.setattr(OData1CClient, "_make_request", _boom)
    errors: list[str] = []
    with caplog.at_level(logging.WARNING, logger=odata_client.__name__):
        mapping = _resolve_warehouse_mapping(client, [_guid(7)], errors)

    assert mapping == {}
    assert len(errors) == 4                     # one per candidate catalog
    assert all("503 from" in e for e in errors)
    assert "warehouse catalog lookup failed" in caplog.text


def test_warehouse_mapping_still_short_circuits_on_success(monkeypatch):
    client = OData1CClient("http://1c/odata")
    ref = _guid(7)

    def _resp(self, endpoint, params=None, **_kwargs):
        if endpoint == "Catalog_Склады":
            return {"value": [{"Ref_Key": ref, "Code": "С-1", "Description": "Склад"}]}
        raise AssertionError(f"must not query {endpoint} after a hit")

    monkeypatch.setattr(OData1CClient, "_make_request", _resp)
    errors: list[str] = []
    mapping = _resolve_warehouse_mapping(client, [ref], errors)
    assert mapping[ref]["Name"] == "Склад"
    assert errors == []


def test_stock_keeps_partial_nomenclature_mapping_and_reports_the_failure(
    monkeypatch, caplog
):
    """A failing batch used to discard every code already resolved."""
    keys = [_guid(i) for i in range(25)]

    def _fake_get_all(self, entity_name, **_kwargs):
        return [
            {"Номенклатура_Key": k, "СтруктурнаяЕдиница_Key": _guid(900), "КоличествоBalance": 1.0}
            for k in keys
        ]

    seen_chunks = {"n": 0}

    def _fake_make_request(self, endpoint, params=None, **_kwargs):
        if endpoint == "Catalog_Номенклатура":
            seen_chunks["n"] += 1
            if seen_chunks["n"] == 1:
                return {
                    "value": [
                        {"Ref_Key": k, "Code": f"CODE-{k[:8]}", "Description": "N", "Артикул": ""}
                        for k in sorted(keys)[:20]
                    ]
                }
            raise RuntimeError("1c gateway timeout")
        return {"value": []}

    monkeypatch.setattr(OData1CClient, "get_all", _fake_get_all)
    monkeypatch.setattr(OData1CClient, "_make_request", _fake_make_request)

    diagnostics: dict = {}
    with caplog.at_level(logging.ERROR, logger=odata_client.__name__):
        rows = odata_client.get_stock_from_1c_odata(
            base_url="http://1c/odata",
            entity_name="AccumulationRegister_ЗапасыНаСкладах/Balance",
            diagnostics=diagnostics,
        )

    assert diagnostics["nomenclature_keys"] == 25
    assert diagnostics["nomenclature_resolved"] == 20
    assert "gateway timeout" in diagnostics["nomenclature_resolve_error"]
    assert "nomenclature code resolution failed" in caplog.text
    # The 20 codes that did come back are still applied to the balance rows.
    assert sum(1 for r in rows if r["code"].startswith("CODE-")) == 20


def test_stock_diagnostics_are_clean_on_a_healthy_fetch(monkeypatch):
    ref = _guid(1)

    monkeypatch.setattr(
        OData1CClient,
        "get_all",
        lambda self, entity_name, **_k: [
            {"Номенклатура_Key": ref, "СтруктурнаяЕдиница_Key": _guid(9), "КоличествоBalance": 2.0}
        ],
    )
    monkeypatch.setattr(
        OData1CClient,
        "_make_request",
        lambda self, endpoint, params=None, **_k: (
            {"value": [{"Ref_Key": ref, "Code": "I-1", "Description": "Item", "Артикул": ""}]}
            if endpoint == "Catalog_Номенклатура"
            else {"value": [{"Ref_Key": _guid(9), "Code": "W-1", "Description": "Склад"}]}
        ),
    )
    diagnostics: dict = {}
    rows = odata_client.get_stock_from_1c_odata(
        base_url="http://1c/odata",
        entity_name="AccumulationRegister_ЗапасыНаСкладах/Balance",
        diagnostics=diagnostics,
    )
    assert rows[0]["code"] == "I-1"
    assert diagnostics == {
        "truncated": False,
        "nomenclature_resolve_error": None,
        "nomenclature_keys": 1,
        "nomenclature_resolved": 1,
        "warehouse_resolve_errors": [],
    }


def test_stock_diagnostics_report_truncated_balance(monkeypatch, caplog):
    def _fake_get_all(self, entity_name, **_kwargs):
        self.last_result_truncated = True
        return []

    monkeypatch.setattr(OData1CClient, "get_all", _fake_get_all)
    diagnostics: dict = {}
    with caplog.at_level(logging.ERROR, logger=odata_client.__name__):
        odata_client.get_stock_from_1c_odata(
            base_url="http://1c/odata",
            entity_name="AccumulationRegister_ЗапасыНаСкладах/Balance",
            diagnostics=diagnostics,
        )
    assert diagnostics["truncated"] is True
    assert "TRUNCATED" in caplog.text


def test_stock_without_diagnostics_argument_still_works(monkeypatch):
    monkeypatch.setattr(OData1CClient, "get_all", lambda self, entity_name, **_k: [])
    assert odata_client.get_stock_from_1c_odata(
        base_url="http://1c/odata",
        entity_name="AccumulationRegister_ЗапасыНаСкладах/Balance",
    ) == []
