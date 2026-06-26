"""Оркестраторы writeback_restage/move/add: read-modify-write поверх фейк-клиента 1С."""
from __future__ import annotations

import re

import pytest

from app.services import spec_writeback_1c as wb


def _row(nom, qty, stage, spec=None, **extra):
    base = {
        "Ref_Key": f"row-{nom}",
        "LineNumber": "1",
        "Номенклатура_Key": nom,
        "Количество": qty,
        "Этап_Key": stage,
        "ТипСтрокиСостава": "Материал",
        "Спецификация_Key": spec or wb.ZERO_GUID,
        "ЕдиницаИзмерения": "PCE",
        "СпособПополнения": "Закупка",
        "СкладПоУмолчанию": "wh-1",
    }
    base.update(extra)
    return base


class _FakeClient:
    def __init__(self, specs):
        self.specs = specs  # {ref: [rows]}
        self.patches = []

    def get_all(self, entity, filter_query=None, select_fields=None):
        m = re.search(r"guid'([^']+)'", filter_query or "")
        ref = m.group(1) if m else None
        if ref in self.specs:
            return [{"Ref_Key": ref, "Состав": self.specs[ref]}]
        return []

    def patch(self, endpoint, payload):
        self.patches.append((endpoint, payload))
        return {"status": "ok"}


# ---------- restage ----------

def test_writeback_restage_changes_one_stage():
    client = _FakeClient({"S": [_row("NOM-A", 1, "ST-OLD"), _row("NOM-B", 2, "ST-OLD")]})
    res = wb.writeback_restage(
        client, spec_ref="S", nomenclature_key="nom-a", child_spec_key=None,
        new_stage_key="ST-NEW", dry_run=False,
    )
    assert res["changed"] == 1
    assert len(client.patches) == 1
    _, payload = client.patches[0]
    changed = next(r for r in payload["Состав"] if r["Номенклатура_Key"] == "NOM-A")
    untouched = next(r for r in payload["Состав"] if r["Номенклатура_Key"] == "NOM-B")
    assert changed["Этап_Key"] == "ST-NEW"
    assert untouched["Этап_Key"] == "ST-OLD"


def test_writeback_restage_dry_run_no_patch():
    client = _FakeClient({"S": [_row("NOM-A", 1, "ST-OLD")]})
    res = wb.writeback_restage(
        client, spec_ref="S", nomenclature_key="nom-a", child_spec_key=None,
        new_stage_key="ST-NEW", dry_run=True,
    )
    assert res["patch"]["dry_run"] is True
    assert client.patches == []


def test_writeback_restage_raises_when_no_match():
    client = _FakeClient({"S": [_row("NOM-A", 1, "ST-OLD")]})
    with pytest.raises(wb.SpecWritebackError):
        wb.writeback_restage(
            client, spec_ref="S", nomenclature_key="missing", child_spec_key=None,
            new_stage_key="ST-NEW", dry_run=False,
        )


# ---------- move ----------

def test_writeback_move_appends_target_then_pops_source():
    client = _FakeClient({
        "SRC": [_row("NOM-A", 2, "ST-A"), _row("NOM-B", 1, "ST-A")],
        "DST": [_row("NOM-C", 1, "ST-D")],
    })
    res = wb.writeback_move(
        client, source_spec_ref="SRC", target_spec_ref="DST",
        nomenclature_key="nom-a", child_spec_key=None, dry_run=False,
    )
    # порядок патчей: сначала target (append), потом source (pop)
    assert [p[0] for p in client.patches] == [
        "Catalog_Спецификации(guid'DST')", "Catalog_Спецификации(guid'SRC')",
    ]
    _, dst_payload = client.patches[0]
    _, src_payload = client.patches[1]
    moved = next(r for r in dst_payload["Состав"] if r["Номенклатура_Key"] == "NOM-A")
    assert moved["Этап_Key"] == "ST-D"            # этап как у соседей в target
    assert moved["СкладПоУмолчанию"] == "wh-1"    # поля строки переехали целиком
    assert "NOM-A" not in [r["Номенклатура_Key"] for r in src_payload["Состав"]]
    assert res["target_rows"] == 2 and res["source_rows"] == 1


def test_writeback_move_raises_when_source_missing_row():
    client = _FakeClient({"SRC": [_row("NOM-B", 1, "ST")], "DST": []})
    with pytest.raises(wb.SpecWritebackError):
        wb.writeback_move(
            client, source_spec_ref="SRC", target_spec_ref="DST",
            nomenclature_key="nom-a", child_spec_key=None, dry_run=False,
        )


# ---------- add ----------

def test_writeback_add_appends_full_row_from_template():
    client = _FakeClient({"S": [_row("NOM-A", 1, "ST-A")]})
    res = wb.writeback_add(
        client, spec_ref="S", nomenclature_key="NOM-NEW", unit_key="KG",
        quantity=5, stage_key="ST-A", component_type="Материал", dry_run=False,
    )
    assert res["rows"] == 2
    _, payload = client.patches[0]
    added = next(r for r in payload["Состав"] if r["Номенклатура_Key"] == "NOM-NEW")
    assert added["Ref_Key"] == ""                 # 1С присвоит свой
    assert added["Количество"] == 5
    assert added["ЕдиницаИзмерения"] == "KG"
    assert added["Этап_Key"] == "ST-A"
    assert added["Спецификация_Key"] == wb.ZERO_GUID
    # структурное поле из шаблона сохранилось
    assert added["СпособПополнения"] == "Закупка"


def test_writeback_add_pins_child_spec_when_given():
    client = _FakeClient({"S": [_row("NOM-A", 1, "ST-A")]})
    wb.writeback_add(
        client, spec_ref="S", nomenclature_key="NOM-SUB", unit_key=None,
        quantity=1, stage_key="ST-A", component_type="Сборка",
        child_spec_key="CHILD-REF", dry_run=False,
    )
    _, payload = client.patches[0]
    added = next(r for r in payload["Состав"] if r["Номенклатура_Key"] == "NOM-SUB")
    assert added["Спецификация_Key"] == "CHILD-REF"
    assert added["ТипСтрокиСостава"] == "Сборка"
