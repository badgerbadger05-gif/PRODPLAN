"""Write-back состава в 1С: read-modify-write, сохранение нетронутых полей строк."""
from __future__ import annotations

import re

import pytest

from app.services import spec_writeback_1c as wb


def _row(nom, qty, stage, spec, **extra):
    base = {
        "Ref_Key": f"row-{nom}-{spec or 'none'}",
        "Номенклатура_Key": nom,
        "Количество": qty,
        "Этап_Key": stage,
        "ТипСтрокиСостава": "Сборка",
        "Спецификация_Key": spec or "00000000-0000-0000-0000-000000000000",
        # поля, которых PRODPLAN не хранит — должны пережить любую правку:
        "СпособПополнения": "Производство",
        "СкладПоУмолчанию": "wh-1",
        "ЕдиницаИзмерения": "PCE",
    }
    base.update(extra)
    return base


# ---------- pure helpers ----------

def test_set_stage_preserves_untouched_fields():
    target = _row("NOM-A", 3, "ST-OLD", "SP-X")
    other = _row("NOM-B", 1, "ST-Z", None)
    other_before = dict(other)

    rows, changed = wb.set_stage_on_rows(
        [target, other], nomenclature_key="nom-a", child_spec_key="sp-x", new_stage_key="ST-NEW"
    )

    assert changed == 1
    t = next(r for r in rows if r["Номенклатура_Key"] == "NOM-A")
    assert t["Этап_Key"] == "ST-NEW"
    # все прочие поля целевой строки нетронуты
    for k in ("СпособПополнения", "СкладПоУмолчанию", "ЕдиницаИзмерения", "Количество", "Спецификация_Key"):
        assert t[k] == target[k]
    # чужая строка не изменилась вообще
    o = next(r for r in rows if r["Номенклатура_Key"] == "NOM-B")
    assert o == other_before


def test_match_normalizes_case_and_zero_guid():
    # Спецификация_Key пустой = нулевой GUID; ключ запроса пустой -> совпадает
    target = _row("nom-a", 1, "ST", None)
    rows, changed = wb.set_stage_on_rows(
        [target], nomenclature_key="NOM-A", child_spec_key=None, new_stage_key="ST2"
    )
    assert changed == 1 and rows[0]["Этап_Key"] == "ST2"


def test_pop_row_returns_full_dict_and_removes_one():
    a = _row("NOM-A", 2, "ST", "SP-X")
    b = _row("NOM-B", 1, "ST", None)
    rows, popped = wb.pop_row([a, b], nomenclature_key="nom-a", child_spec_key="sp-x")

    assert popped is not None
    assert popped["СпособПополнения"] == "Производство"  # полный словарь со всеми полями
    assert [r["Номенклатура_Key"] for r in rows] == ["NOM-B"]


def test_pop_row_no_match_returns_none():
    rows, popped = wb.pop_row([_row("NOM-A", 1, "ST", "SP-X")], nomenclature_key="nom-a", child_spec_key="other")
    assert popped is None and len(rows) == 1


def test_repoint_child_spec_changes_only_spec_key():
    target = _row("NOM-A", 1, "ST", "SP-OLD")
    rows, changed = wb.repoint_child_spec(
        [target], nomenclature_key="nom-a", old_spec_key="sp-old", new_spec_key="SP-NEW"
    )
    assert changed == 1
    assert rows[0]["Спецификация_Key"] == "SP-NEW"
    for k in ("Этап_Key", "Количество", "СпособПополнения", "СкладПоУмолчанию"):
        assert rows[0][k] == target[k]


def test_renumber_sets_sequential_linenumber():
    rows = wb.renumber([_row("A", 1, "S", None), _row("B", 1, "S", None), _row("C", 1, "S", None)])
    assert [r["LineNumber"] for r in rows] == ["1", "2", "3"]


# ---------- orchestration ----------

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


def test_dry_run_does_not_patch():
    client = _FakeClient({"spec-1": [_row("NOM-A", 1, "ST", "SP-X")]})
    sb = wb.SpecWriteback(client, dry_run=True)

    res = sb.apply_to_sostav(
        "spec-1",
        lambda rows: wb.set_stage_on_rows(rows, nomenclature_key="nom-a", child_spec_key="sp-x", new_stage_key="ST2")[0],
    )

    assert res["dry_run"] is True
    assert client.patches == []  # ни одного реального PATCH
    assert res["would_patch"]["Состав"][0]["Этап_Key"] == "ST2"
    assert res["would_patch"]["Состав"][0]["LineNumber"] == "1"


def test_apply_patches_when_not_dry_run():
    client = _FakeClient({"spec-1": [_row("NOM-A", 1, "ST", "SP-X")]})
    sb = wb.SpecWriteback(client, dry_run=False)

    res = sb.apply_to_sostav(
        "spec-1",
        lambda rows: wb.set_stage_on_rows(rows, nomenclature_key="nom-a", child_spec_key="sp-x", new_stage_key="ST2")[0],
    )

    assert res["dry_run"] is False
    assert len(client.patches) == 1
    endpoint, payload = client.patches[0]
    assert endpoint == "Catalog_Спецификации(guid'spec-1')"
    assert payload["Состав"][0]["Этап_Key"] == "ST2"


def test_read_sostav_raises_when_missing():
    client = _FakeClient({})
    sb = wb.SpecWriteback(client, dry_run=True)
    with pytest.raises(ValueError):
        sb.read_sostav("nope")


def test_move_composition_preserves_fields_across_specs():
    """Перенос строки из A в B: поля строки (склад/способ) переезжают целиком."""
    src = [_row("NOM-A", 2, "ST-A", "SP-X"), _row("NOM-B", 1, "ST", None)]
    dst = [_row("NOM-C", 1, "ST-D", None)]
    client = _FakeClient({"A": src, "B": dst})
    sb = wb.SpecWriteback(client, dry_run=False)

    src_rows = sb.read_sostav("A")
    remaining, moved = wb.pop_row(src_rows, nomenclature_key="nom-a", child_spec_key="sp-x")
    assert moved["СкладПоУмолчанию"] == "wh-1"
    dst_rows = wb.append_row(sb.read_sostav("B"), moved)

    sb.patch_sostav("A", remaining)
    sb.patch_sostav("B", dst_rows)

    assert len(client.patches) == 2
    # B теперь содержит перенесённую строку со всеми полями
    _, b_payload = client.patches[1]
    moved_in_b = next(r for r in b_payload["Состав"] if r["Номенклатура_Key"] == "NOM-A")
    assert moved_in_b["СкладПоУмолчанию"] == "wh-1"
    assert moved_in_b["СпособПополнения"] == "Производство"
