"""Write-back состава в 1С: read-modify-write, сохранение нетронутых полей строк."""
from __future__ import annotations

import copy
import re

import pytest

from app.services import spec_writeback_1c as wb


PARENT_SPEC = "10000000-0000-0000-0000-000000000001"
PARENT_OWNER = "20000000-0000-0000-0000-000000000001"
ITEM_A = "30000000-0000-0000-0000-000000000001"
ITEM_B = "30000000-0000-0000-0000-000000000002"
CHAR_A = "40000000-0000-0000-0000-000000000001"
TARGET_A = "50000000-0000-0000-0000-000000000001"
TARGET_B = "50000000-0000-0000-0000-000000000002"
OLD_PIN = "60000000-0000-0000-0000-000000000001"
KIND = "70000000-0000-0000-0000-000000000001"
OPERATION = "80000000-0000-0000-0000-000000000001"
MATERIAL = "90000000-0000-0000-0000-000000000001"
UOM = "90000000-0000-0000-0000-000000000002"
OPERATION_B = "80000000-0000-0000-0000-000000000002"


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


# ---------- guarded category migration writers ----------


def _full_spec(
    ref,
    owner,
    rows=None,
    *,
    version="v1",
    description="spec",
    characteristic=wb.ZERO_GUID,
    etag='W/"v1"',
):
    return {
        "Ref_Key": ref,
        "Owner_Key": owner,
        "DataVersion": version,
        "@odata.etag": etag,
        "Code": "AUTO",
        "Description": description,
        "DeletionMark": False,
        "Недействителен": False,
        "ЭтоШаблон": False,
        "Predefined": False,
        "PredefinedDataName": "",
        "ВидПроизводства_Key": KIND,
        "ХарактеристикаПродукции_Key": characteristic,
        "Состав": copy.deepcopy(rows or []),
        "Операции": [],
        "UnknownHeader": {"must": "survive"},
    }


def _guarded_row(line, item, characteristic, row_type, pin, **extra):
    row = {
        "LineNumber": str(line),
        "ТипСтрокиСостава": row_type,
        "Номенклатура_Key": item,
        "Характеристика_Key": characteristic,
        "Спецификация_Key": pin,
        "Количество": 1.25,
        "UnknownRowField": {"keep": [1, 2, 3]},
    }
    row.update(extra)
    return row


class _GuardedClient:
    def __init__(self, specs, defaults=None):
        self.specs = copy.deepcopy(specs)
        self.defaults = copy.deepcopy(defaults or [])
        self.patches = []
        self.posts = []
        self.spec_reads = {}

    def get_all(self, entity, filter_query=None, select_fields=None, **_kwargs):
        if entity == wb.SPEC_ENTITY:
            match = re.search(r"guid'([^']+)'", filter_query or "")
            ref = match.group(1).lower() if match else ""
            self.spec_reads[ref] = self.spec_reads.get(ref, 0) + 1
            record = self.specs.get(ref)
            return [copy.deepcopy(record)] if record else []
        if entity == wb.DEFAULT_SPEC_ENTITY:
            match = re.search(r"guid'([^']+)'", filter_query or "")
            owner = match.group(1).lower() if match else ""
            return [
                copy.deepcopy(row)
                for row in self.defaults
                if row["Номенклатура_Key"].lower() == owner
            ]
        raise AssertionError(entity)

    def patch(self, endpoint, payload, if_match=None):
        self.patches.append((endpoint, copy.deepcopy(payload), if_match))
        if endpoint.startswith(wb.SPEC_ENTITY):
            ref = re.search(r"guid'([^']+)'", endpoint).group(1).lower()
            self.specs[ref].update(copy.deepcopy(payload))
            self.specs[ref]["DataVersion"] = "v2"
            self.specs[ref]["@odata.etag"] = 'W/"v2"'
        else:
            refs = re.findall(r"guid'([^']+)'", endpoint)
            item, characteristic = (value.lower() for value in refs)
            row = next(
                row
                for row in self.defaults
                if row["Номенклатура_Key"].lower() == item
                and row["Характеристика_Key"].lower() == characteristic
            )
            row.update(copy.deepcopy(payload))
        return {"ok": True}

    def post(self, endpoint, payload):
        self.posts.append((endpoint, copy.deepcopy(payload)))
        if endpoint == wb.SPEC_ENTITY:
            record = copy.deepcopy(payload)
            record["DataVersion"] = "created-v1"
            record["Code"] = "SERVER-CODE"
            record["Predefined"] = False
            record["PredefinedDataName"] = ""
            for value in record.values():
                if isinstance(value, list):
                    for row in value:
                        if isinstance(row, dict):
                            row.pop("Ref_Key", None)
            self.specs[record["Ref_Key"].lower()] = record
        elif endpoint == wb.DEFAULT_SPEC_ENTITY:
            self.defaults.append(copy.deepcopy(payload))
        else:
            raise AssertionError(endpoint)
        return {"ok": True}


class _ReorderingGuardedClient(_GuardedClient):
    def __init__(self, specs, *, mutate_quantity=False):
        super().__init__(specs)
        self.mutate_quantity = mutate_quantity

    def patch(self, endpoint, payload, **kwargs):
        response = super().patch(endpoint, payload, **kwargs)
        if endpoint.startswith(wb.SPEC_ENTITY):
            ref = re.search(r"guid'([^']+)'", endpoint).group(1).lower()
            record = self.specs[ref]
            record["Состав"] = sorted(
                record["Состав"], key=lambda row: int(row["LineNumber"])
            )
            record["Операции"] = sorted(
                record["Операции"], key=lambda row: int(row["LineNumber"])
            )
            if self.mutate_quantity:
                record["Состав"][-1]["Количество"] += 1
        return response


def _change(line, characteristic, *, item=ITEM_A, target=TARGET_A, old_pin=wb.ZERO_GUID):
    return {
        "line_number": str(line),
        "nomenclature_key": item,
        "characteristic_key": characteristic,
        "old_row_type": "Материал",
        "old_specification_key": old_pin,
        "new_specification_key": target,
    }


def test_guarded_batch_is_one_parent_patch_and_preserves_unknown_fields_and_line_numbers():
    parent_rows = [
        _guarded_row("10", ITEM_A, wb.ZERO_GUID, "Материал", wb.ZERO_GUID),
        _guarded_row("40", ITEM_A, CHAR_A, "Материал", OLD_PIN),
        _guarded_row("90", ITEM_B, wb.ZERO_GUID, "Материал", wb.ZERO_GUID, Untouched=True),
    ]
    client = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, parent_rows),
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
            TARGET_B: _full_spec(TARGET_B, ITEM_A, characteristic=CHAR_A),
        }
    )

    result = wb.writeback_promote_material_rows(
        client,
        parent_spec_ref=PARENT_SPEC,
        row_changes=[
            _change("10", wb.ZERO_GUID),
            _change("40", CHAR_A, target=TARGET_B, old_pin=OLD_PIN),
        ],
        expected_data_version="v1",
        expected_etag='W/"v1"',
        concurrency_runtime_validated=True,
        parent_edit_policy="safe_unused_editable",
        dry_run=False,
    )

    assert result["status"] == "updated"
    assert result["source_count"] == 2
    assert result["validated_target_count"] == 2
    assert client.spec_reads[TARGET_A] == 1
    assert client.spec_reads[TARGET_B] == 1
    assert len(client.patches) == 1
    payload_rows = client.patches[0][1]["Состав"]
    assert [row["LineNumber"] for row in payload_rows] == ["10", "40", "90"]
    assert payload_rows[0]["UnknownRowField"] == {"keep": [1, 2, 3]}
    assert payload_rows[1]["Характеристика_Key"] == CHAR_A
    assert payload_rows[1]["Спецификация_Key"] == TARGET_B
    assert payload_rows[0]["ТипСтрокиСостава"] == "Сборка"
    assert payload_rows[0]["Спецификация_Key"] == TARGET_A
    assert payload_rows[2] == parent_rows[2]


def test_guarded_batch_accepts_readback_reordering_by_numeric_line_number():
    row_two = _guarded_row(
        "2", ITEM_B, wb.ZERO_GUID, "Сборка", TARGET_B, UnknownUntouched="keep"
    )
    row_one = _guarded_row(
        "1", ITEM_A, wb.ZERO_GUID, "Материал", wb.ZERO_GUID, UnknownTarget="keep"
    )
    parent = _full_spec(PARENT_SPEC, PARENT_OWNER, [row_two, row_one])
    parent["Операции"] = [
        {"LineNumber": "2", "Операция_Key": OPERATION, "Unknown": "second"},
        {"LineNumber": "1", "Операция_Key": OPERATION, "Unknown": "first"},
    ]
    client = _ReorderingGuardedClient(
        {
            PARENT_SPEC: parent,
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
        }
    )

    result = wb.writeback_promote_material_rows(
        client,
        parent_spec_ref=PARENT_SPEC,
        row_changes=[_change("1", wb.ZERO_GUID)],
        expected_data_version="v1",
        expected_etag='W/"v1"',
        concurrency_runtime_validated=True,
        parent_edit_policy="safe_unused_editable",
        dry_run=False,
    )

    assert result["status"] == "updated"
    payload_rows = client.patches[0][1]["Состав"]
    assert [row["LineNumber"] for row in payload_rows] == ["2", "1"]
    assert payload_rows[0]["UnknownUntouched"] == "keep"
    assert payload_rows[1]["UnknownTarget"] == "keep"
    assert [row["LineNumber"] for row in client.specs[PARENT_SPEC]["Состав"]] == [
        "1",
        "2",
    ]


def test_guarded_batch_rejects_reordered_readback_with_changed_quantity():
    row_two = _guarded_row("2", ITEM_B, wb.ZERO_GUID, "Сборка", TARGET_B)
    row_one = _guarded_row("1", ITEM_A, wb.ZERO_GUID, "Материал", wb.ZERO_GUID)
    parent = _full_spec(PARENT_SPEC, PARENT_OWNER, [row_two, row_one])
    parent["Операции"] = [{"LineNumber": "1", "Операция_Key": OPERATION}]
    client = _ReorderingGuardedClient(
        {
            PARENT_SPEC: parent,
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
        },
        mutate_quantity=True,
    )

    with pytest.raises(wb.SpecWritebackError, match="read-back изменил объект"):
        wb.writeback_promote_material_rows(
            client,
            parent_spec_ref=PARENT_SPEC,
            row_changes=[_change("1", wb.ZERO_GUID)],
            expected_data_version="v1",
            expected_etag='W/"v1"',
            concurrency_runtime_validated=True,
            parent_edit_policy="safe_unused_editable",
            dry_run=False,
        )


def test_guarded_batch_rejects_duplicate_line_numbers_before_write():
    rows = [
        _guarded_row("1", ITEM_A, wb.ZERO_GUID, "Материал", wb.ZERO_GUID),
        _guarded_row("1", ITEM_B, wb.ZERO_GUID, "Материал", wb.ZERO_GUID),
    ]
    client = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, rows),
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
        }
    )
    with pytest.raises(wb.SpecWritebackError, match="дублирующийся LineNumber"):
        wb.writeback_promote_material_rows(
            client,
            parent_spec_ref=PARENT_SPEC,
            row_changes=[_change("1", wb.ZERO_GUID)],
            expected_data_version="v1",
            dry_run=False,
        )
    assert client.patches == []


def test_guarded_batch_rejects_stale_data_version_before_patch():
    row = _guarded_row("1", ITEM_A, wb.ZERO_GUID, "Материал", wb.ZERO_GUID)
    client = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, [row], version="fresh"),
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
        }
    )

    with pytest.raises(wb.SpecWritebackError, match="DataVersion"):
        wb.writeback_promote_material_rows(
            client,
            parent_spec_ref=PARENT_SPEC,
            row_changes=[_change("1", wb.ZERO_GUID)],
            expected_data_version="stale",
            dry_run=False,
        )
    assert client.patches == []


def test_guarded_batch_is_resumable_when_exact_target_is_already_applied():
    row = _guarded_row("1", ITEM_A, wb.ZERO_GUID, "Сборка", TARGET_A)
    client = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, [row], version="new"),
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
        }
    )

    result = wb.writeback_promote_material_rows(
        client,
        parent_spec_ref=PARENT_SPEC,
        row_changes=[_change("1", wb.ZERO_GUID)],
        expected_data_version="old-and-now-stale",
        dry_run=False,
    )

    assert result["status"] == "already_applied"
    assert result["already_applied_count"] == 1
    assert client.patches == []


def test_guarded_batch_pins_existing_assembly_and_guards_expected_old_pin():
    row = _guarded_row(
        "1",
        ITEM_A,
        wb.ZERO_GUID,
        "Сборка",
        wb.ZERO_GUID,
        UnknownAssemblyField="preserve",
    )
    client = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, [row]),
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
        }
    )
    change = _change("1", wb.ZERO_GUID)
    change["old_row_type"] = "Сборка"

    result = wb.writeback_promote_material_rows(
        client,
        parent_spec_ref=PARENT_SPEC,
        row_changes=[change],
        expected_data_version="v1",
        expected_etag='W/"v1"',
        concurrency_runtime_validated=True,
        parent_edit_policy="safe_unused_editable",
        dry_run=False,
    )

    assert result["status"] == "updated"
    written = client.patches[0][1]["Состав"][0]
    assert written["ТипСтрокиСостава"] == "Сборка"
    assert written["Спецификация_Key"] == TARGET_A
    assert written["UnknownAssemblyField"] == "preserve"

    drift_client = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, [row]),
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
        }
    )
    wrong_old_pin = dict(change)
    wrong_old_pin["old_specification_key"] = OLD_PIN
    with pytest.raises(wb.SpecWritebackError, match="drift строки"):
        wb.writeback_promote_material_rows(
            drift_client,
            parent_spec_ref=PARENT_SPEC,
            row_changes=[wrong_old_pin],
            expected_data_version="v1",
            dry_run=False,
        )
    assert drift_client.patches == []


def test_guarded_batch_rejects_illegal_row_type_and_malformed_reference():
    client = _GuardedClient({})
    illegal = _change("1", wb.ZERO_GUID)
    illegal["old_row_type"] = "Услуга"
    with pytest.raises(wb.SpecWritebackError, match="недопустимый переход"):
        wb.writeback_promote_material_rows(
            client,
            parent_spec_ref=PARENT_SPEC,
            row_changes=[illegal],
            expected_data_version="v1",
        )
    malformed = _change("1", wb.ZERO_GUID)
    malformed["new_specification_key"] = "not-a-guid"
    with pytest.raises(wb.SpecWritebackError, match="некорректный GUID"):
        wb.writeback_promote_material_rows(
            client,
            parent_spec_ref=PARENT_SPEC,
            row_changes=[malformed],
            expected_data_version="v1",
        )


def test_guarded_batch_rejects_cross_owner_child_and_direct_owner_cycle():
    row = _guarded_row("1", ITEM_A, wb.ZERO_GUID, "Материал", wb.ZERO_GUID)
    cross_owner = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, [row]),
            TARGET_A: _full_spec(TARGET_A, ITEM_B),
        }
    )
    with pytest.raises(wb.SpecWritebackError, match="owner mismatch"):
        wb.writeback_promote_material_rows(
            cross_owner,
            parent_spec_ref=PARENT_SPEC,
            row_changes=[_change("1", wb.ZERO_GUID)],
            expected_data_version="v1",
        )

    self_row = _guarded_row("1", PARENT_OWNER, wb.ZERO_GUID, "Материал", wb.ZERO_GUID)
    self_owner = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, [self_row]),
            TARGET_A: _full_spec(TARGET_A, PARENT_OWNER),
        }
    )
    with pytest.raises(wb.SpecWritebackError, match="прямой цикл"):
        wb.writeback_promote_material_rows(
            self_owner,
            parent_spec_ref=PARENT_SPEC,
            row_changes=[_change("1", wb.ZERO_GUID, item=PARENT_OWNER)],
            expected_data_version="v1",
        )


def _creation_template():
    return {
        "odata.metadata": "readonly",
        "Ref_Key": PARENT_SPEC,
        "Owner_Key": PARENT_OWNER,
        "Code": "DROP-ME",
        "DataVersion": "DROP-ME",
        "Predefined": False,
        "PredefinedDataName": "",
        "Description": "template",
        "DeletionMark": False,
        "Недействителен": False,
        "ЭтоШаблон": False,
        "ВидПроизводства_Key": KIND,
        "Owner@navigationLinkUrl": "drop",
        "Состав": [
            _guarded_row("7", MATERIAL, wb.ZERO_GUID, "Материал", wb.ZERO_GUID)
        ],
        "Операции": [
            {
                "LineNumber": "3",
                "Операция_Key": OPERATION,
                "НормаВремени": 1,
                "UnknownOperation": "keep",
            }
        ],
        "UnknownHeader": "keep",
    }


def test_create_specification_uses_preallocated_uuid_filters_readonly_and_is_idempotent():
    client = _GuardedClient({})
    result = wb.writeback_create_specification(
        client,
        template=_creation_template(),
        new_spec_ref=TARGET_A,
        new_owner_ref=ITEM_A,
        material_ref=MATERIAL,
        material_characteristic_ref=CHAR_A,
        material_quantity=2.5,
        operation_ref=OPERATION,
        operation_time=0.75,
        description="new spec",
        dry_run=False,
    )

    assert result["status"] == "created"
    assert len(client.posts) == 1
    endpoint, payload = client.posts[0]
    assert endpoint == wb.SPEC_ENTITY
    assert payload["Ref_Key"] == TARGET_A
    assert payload["Состав"][0]["Ref_Key"] == TARGET_A
    assert payload["Операции"][0]["Ref_Key"] == TARGET_A
    assert payload["UnknownHeader"] == "keep"
    assert payload["Операции"][0]["UnknownOperation"] == "keep"
    for readonly in ("Code", "DataVersion", "Predefined", "PredefinedDataName"):
        assert readonly not in payload
    assert "odata.metadata" not in payload
    assert all("@" not in key for key in payload)

    repeated = wb.writeback_create_specification(
        client,
        template=_creation_template(),
        new_spec_ref=TARGET_A,
        new_owner_ref=ITEM_A,
        material_ref=MATERIAL,
        material_characteristic_ref=CHAR_A,
        material_quantity=2.5,
        operation_ref=OPERATION,
        operation_time=0.75,
        description="new spec",
        dry_run=False,
    )
    assert repeated["status"] == "already_applied"
    assert len(client.posts) == 1


@pytest.mark.parametrize(
    ("quantity", "operation_time"),
    [
        (float("nan"), 1),
        (float("inf"), 1),
        (1, float("nan")),
        (1, float("-inf")),
    ],
)
def test_create_specification_rejects_non_finite_numbers(quantity, operation_time):
    with pytest.raises(wb.SpecWritebackError, match="положительными"):
        wb.build_specification_create_payload(
            _creation_template(),
            new_spec_ref=TARGET_A,
            new_owner_ref=ITEM_A,
            material_ref=MATERIAL,
            material_characteristic_ref=wb.ZERO_GUID,
            material_quantity=quantity,
            operation_ref=OPERATION,
            operation_time=operation_time,
        )


def test_default_writer_uses_full_characteristic_identity_and_old_value_guard():
    defaults = [
        {
            "Номенклатура_Key": ITEM_A,
            "Характеристика_Key": wb.ZERO_GUID,
            "Спецификация_Key": OLD_PIN,
            "Unknown": "keep",
        },
        {
            "Номенклатура_Key": ITEM_A,
            "Характеристика_Key": CHAR_A,
            "Спецификация_Key": TARGET_B,
            "Unknown": "other-characteristic",
        },
    ]
    client = _GuardedClient({TARGET_A: _full_spec(TARGET_A, ITEM_A)}, defaults)

    result = wb.writeback_default_specification(
        client,
        nomenclature_key=ITEM_A,
        characteristic_key=wb.ZERO_GUID,
        target_spec_ref=TARGET_A,
        expected_old_spec_ref=OLD_PIN,
        dry_run=False,
    )

    assert result["status"] == "updated"
    assert result["method"] == "patch"
    zero_row = next(row for row in client.defaults if row["Характеристика_Key"] == wb.ZERO_GUID)
    char_row = next(row for row in client.defaults if row["Характеристика_Key"] == CHAR_A)
    assert zero_row["Спецификация_Key"] == TARGET_A
    assert zero_row["Unknown"] == "keep"
    assert char_row["Спецификация_Key"] == TARGET_B
    assert char_row["Unknown"] == "other-characteristic"


def test_default_writer_posts_only_when_exact_composite_key_is_expected_absent():
    client = _GuardedClient(
        {TARGET_A: _full_spec(TARGET_A, ITEM_A, characteristic=CHAR_A)}
    )
    result = wb.writeback_default_specification(
        client,
        nomenclature_key=ITEM_A,
        characteristic_key=CHAR_A,
        target_spec_ref=TARGET_A,
        expect_absent=True,
        dry_run=False,
    )
    assert result["status"] == "created"
    assert client.posts == [
        (
            wb.DEFAULT_SPEC_ENTITY,
            {
                "Номенклатура_Key": ITEM_A,
                "Характеристика_Key": CHAR_A,
                "Спецификация_Key": TARGET_A,
            },
        )
    ]


def test_default_writer_rejects_target_with_other_product_characteristic():
    client = _GuardedClient({TARGET_A: _full_spec(TARGET_A, ITEM_A)})
    with pytest.raises(wb.SpecWritebackError, match="characteristic mismatch"):
        wb.writeback_default_specification(
            client,
            nomenclature_key=ITEM_A,
            characteristic_key=CHAR_A,
            target_spec_ref=TARGET_A,
            expect_absent=True,
        )


# ---------- lossless merged create / guarded compensation ----------


def _merged_inputs():
    header = _creation_template()
    header["UnknownMergedHeader"] = {"preserve": [1, 2]}
    composition = []
    for index, item in enumerate((MATERIAL, ITEM_B, PARENT_OWNER), start=1):
        composition.append(
            {
                "Ref_Key": OLD_PIN,
                "LineNumber": str(index * 10),
                "ТипСтрокиСостава": "Материал",
                "Номенклатура_Key": item,
                "Характеристика_Key": wb.ZERO_GUID,
                "Спецификация_Key": wb.ZERO_GUID,
                "ЕдиницаИзмерения": UOM,
                "Количество": index * 1.25,
                "КоличествоПродукции": 2.5,
                "Этап_Key": KIND,
                "UnknownMaterial": {"index": index},
            }
        )
    operations = [
        {
            "Ref_Key": OLD_PIN,
            "LineNumber": "1",
            "Операция_Key": OPERATION,
            "НормаВремени": 0.5,
            "Количество": 1,
            "КоличествоПродукции": 2.5,
            "Этап_Key": KIND,
            "UnknownOperation": "first occurrence",
        },
        {
            "Ref_Key": OLD_PIN,
            "LineNumber": "2",
            "Операция_Key": OPERATION,
            "НормаВремени": 0.75,
            "Количество": 2,
            "КоличествоПродукции": 2.5,
            "Этап_Key": KIND,
            "UnknownOperation": "second occurrence",
        },
        {
            "Ref_Key": OLD_PIN,
            "LineNumber": "3",
            "Операция_Key": OPERATION_B,
            "НормаВремени": 1.25,
            "Количество": 3,
            "КоличествоПродукции": 2.5,
            "Этап_Key": KIND,
            "UnknownOperation": "third",
        },
    ]
    provenance = []
    for index, operation in enumerate(operations, start=1):
        provenance.append(
            {
                "source_spec_ref": OLD_PIN,
                "source_operation_ref": operation["Операция_Key"],
                "source_line_number": operation["LineNumber"],
                "source_occurrence": index,
                "source_edge_identity": f"edge:{index}",
                "target_occurrence": index,
            }
        )
    return header, composition, operations, provenance


def _merged_kwargs():
    header, composition, operations, provenance = _merged_inputs()
    return {
        "header_template": header,
        "new_spec_ref": TARGET_A,
        "new_owner_ref": ITEM_A,
        "production_kind_ref": KIND,
        "product_characteristic_ref": wb.ZERO_GUID,
        "composition_rows": composition,
        "operation_rows": operations,
        "operation_provenance": provenance,
        "description": "merged",
    }


def test_merged_create_preserves_three_rows_three_ops_unknown_fields_and_denominators():
    payload = wb.build_merged_specification_create_payload(**_merged_kwargs())
    assert payload["Ref_Key"] == TARGET_A
    assert payload["UnknownMergedHeader"] == {"preserve": [1, 2]}
    assert len(payload["Состав"]) == len(payload["Операции"]) == 3
    assert [row["КоличествоПродукции"] for row in payload["Состав"]] == [2.5] * 3
    assert [row["КоличествоПродукции"] for row in payload["Операции"]] == [2.5] * 3
    assert [row["Операция_Key"] for row in payload["Операции"]] == [
        OPERATION,
        OPERATION,
        OPERATION_B,
    ]
    assert payload["Операции"][1]["UnknownOperation"] == "second occurrence"
    assert all(row["Ref_Key"] == TARGET_A for row in payload["Состав"] + payload["Операции"])
    for readonly in ("Code", "DataVersion", "Predefined", "PredefinedDataName"):
        assert readonly not in payload


def test_merged_create_preview_is_default_and_never_posts():
    client = _GuardedClient({})
    result = wb.writeback_create_merged_specification(client, **_merged_kwargs())
    assert result["status"] == "preview"
    assert client.posts == []


def test_merged_create_is_uuid_idempotent_and_rejects_collision():
    client = _GuardedClient({})
    kwargs = _merged_kwargs()
    created = wb.writeback_create_merged_specification(
        client, **kwargs, mutation_mode=wb.MUTATION_APPLY_GUARDED
    )
    assert created["status"] == "created"
    repeated = wb.writeback_create_merged_specification(
        client, **kwargs, mutation_mode=wb.MUTATION_APPLY_GUARDED
    )
    assert repeated["status"] == "already_applied"
    assert len(client.posts) == 1

    collision = _GuardedClient({TARGET_A: _full_spec(TARGET_A, ITEM_A)})
    with pytest.raises(wb.SpecWritebackError, match="read-back"):
        wb.writeback_create_merged_specification(collision, **kwargs)


def test_merged_create_fails_when_post_readback_is_missing():
    class MissingReadbackClient(_GuardedClient):
        def post(self, endpoint, payload):
            self.posts.append((endpoint, copy.deepcopy(payload)))
            return {"ok": True}

    client = MissingReadbackClient({})
    with pytest.raises(wb.SpecWritebackError, match="Ожидалась ровно 1"):
        wb.writeback_create_merged_specification(
            client, **_merged_kwargs(), mutation_mode=wb.MUTATION_APPLY_GUARDED
        )


def test_merged_create_rejects_named_operation_substitution():
    kwargs = _merged_kwargs()
    kwargs["operation_provenance"][0]["source_operation_ref"] = OPERATION_B
    with pytest.raises(wb.SpecWritebackError, match="named operation GUID changed"):
        wb.build_merged_specification_create_payload(**kwargs)


def test_merged_create_preserves_valid_onec_node_row_with_explicit_pin():
    kwargs = _merged_kwargs()
    row = kwargs["composition_rows"][0]
    row["ТипСтрокиСостава"] = wb.NODE_ROW_TYPE
    row["Спецификация_Key"] = TARGET_B
    payload = wb.build_merged_specification_create_payload(**kwargs)
    assert payload["Состав"][0]["ТипСтрокиСостава"] == "Узел"
    assert payload["Состав"][0]["Спецификация_Key"] == TARGET_B


def test_version_clone_can_preserve_empty_operations_or_zero_existing_time():
    empty = _merged_kwargs()
    empty["operation_rows"] = []
    empty["operation_provenance"] = []
    empty["require_operations"] = False
    payload = wb.build_merged_specification_create_payload(**empty)
    assert payload["Операции"] == []

    zero = _merged_kwargs()
    zero["operation_rows"][0]["НормаВремени"] = 0
    zero["allow_zero_operation_time"] = True
    payload = wb.build_merged_specification_create_payload(**zero)
    assert payload["Операции"][0]["НормаВремени"] == 0

    legacy = _merged_kwargs()
    legacy["composition_rows"][0]["Спецификация_Key"] = TARGET_B
    legacy["composition_rows"][1]["ТипСтрокиСостава"] = "Сборка"
    legacy["composition_rows"][1]["Спецификация_Key"] = wb.ZERO_GUID
    legacy["preserve_existing_row_semantics"] = True
    payload = wb.build_merged_specification_create_payload(**legacy)
    assert payload["Состав"][0]["Спецификация_Key"] == TARGET_B
    assert payload["Состав"][1]["Спецификация_Key"] == wb.ZERO_GUID


def test_parent_unknown_or_used_requires_version_and_does_not_patch():
    row = _guarded_row("1", ITEM_A, wb.ZERO_GUID, "Сборка", OLD_PIN)
    client = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, [row]),
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
        }
    )
    change = _change("1", wb.ZERO_GUID, old_pin=OLD_PIN)
    change["old_row_type"] = "Сборка"
    blocked = wb.writeback_promote_material_rows(
        client,
        parent_spec_ref=PARENT_SPEC,
        row_changes=[change],
        expected_data_version="v1",
        parent_edit_policy="unknown",
        dry_run=False,
    )
    assert blocked["status"] == "blocked_requires_parent_version"
    versioned = wb.writeback_promote_material_rows(
        client,
        parent_spec_ref=PARENT_SPEC,
        row_changes=[change],
        expected_data_version="v1",
        parent_edit_policy="used_or_historical",
        dry_run=False,
    )
    assert versioned["status"] == "new_parent_version_required"
    assert client.patches == []


def test_parent_standard_onec_contract_sends_raw_data_version_if_match():
    row = _guarded_row("1", ITEM_A, wb.ZERO_GUID, "Сборка", OLD_PIN)
    client = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, [row], version="RAW-V1"),
            TARGET_A: _full_spec(TARGET_A, ITEM_A),
        }
    )
    change = _change("1", wb.ZERO_GUID, old_pin=OLD_PIN)
    change["old_row_type"] = "Сборка"
    result = wb.writeback_promote_material_rows(
        client,
        parent_spec_ref=PARENT_SPEC,
        row_changes=[change],
        expected_data_version="RAW-V1",
        expected_if_match="RAW-V1",
        concurrency_contract=wb.CONCURRENCY_ONEC_STANDARD_ODATA_V3,
        concurrency_runtime_validated=True,
        parent_edit_policy="safe_unused_editable",
        dry_run=False,
    )
    assert result["status"] == "updated"
    assert client.patches[0][2] == "RAW-V1"


def test_data_version_is_not_reinterpreted_as_etag():
    assert wb.explicit_odata_etag({"DataVersion": "AAAA"}) is None
    assert wb.explicit_odata_etag({"DataVersion": "AAAA", "@odata.etag": 'W/"x"'}) == 'W/"x"'


def test_standard_onec_contract_uses_raw_data_version_as_if_match():
    guard = wb.resolve_write_concurrency_guard(
        {"DataVersion": "AAAAAAAAAAE="},
        contract=wb.CONCURRENCY_ONEC_STANDARD_ODATA_V3,
        runtime_validated=False,
    )
    assert guard.if_match == "AAAAAAAAAAE="
    assert guard.source_field == "DataVersion"
    assert guard.documented_contract is True
    assert guard.runtime_validated is False


def test_guarded_default_preview_and_etag_bound_apply():
    defaults = [
        {
            "Номенклатура_Key": ITEM_A,
            "Характеристика_Key": wb.ZERO_GUID,
            "Спецификация_Key": OLD_PIN,
            "DataVersion": "d1",
            "@odata.etag": 'W/"d1"',
            "Unknown": "keep",
        }
    ]
    client = _GuardedClient({TARGET_A: _full_spec(TARGET_A, ITEM_A)}, defaults)
    preview = wb.writeback_default_specification_guarded(
        client,
        nomenclature_key=ITEM_A,
        characteristic_key=wb.ZERO_GUID,
        target_spec_ref=TARGET_A,
        expected_old_spec_ref=OLD_PIN,
    )
    assert preview["mutation_mode"] == wb.MUTATION_PREVIEW
    assert preview["conditional_write"] == {
        "contract": wb.CONCURRENCY_EXPLICIT_ETAG,
        "source_field": "explicit OData ETag metadata",
        "documented_contract": False,
        "runtime_validated": False,
    }
    assert client.patches == []
    with pytest.raises(wb.SpecWritebackError, match="conditional-write token mismatch"):
        wb.writeback_default_specification_guarded(
            client,
            nomenclature_key=ITEM_A,
            characteristic_key=wb.ZERO_GUID,
            target_spec_ref=TARGET_A,
            expected_old_spec_ref=OLD_PIN,
            expected_etag='W/"wrong"',
            concurrency_runtime_validated=True,
            mutation_mode=wb.MUTATION_APPLY_GUARDED,
        )
    applied = wb.writeback_default_specification_guarded(
        client,
        nomenclature_key=ITEM_A,
        characteristic_key=wb.ZERO_GUID,
        target_spec_ref=TARGET_A,
        expected_old_spec_ref=OLD_PIN,
        expected_etag='W/"d1"',
        concurrency_runtime_validated=True,
        mutation_mode=wb.MUTATION_APPLY_GUARDED,
    )
    assert applied["status"] == "updated"
    assert client.patches[-1][2] == 'W/"d1"'
    assert client.defaults[0]["Unknown"] == "keep"


def test_preview_batch_blocks_only_bad_root_and_performs_no_mutations():
    client = _GuardedClient({})
    valid = {"root_id": "valid", **_merged_kwargs(), "expected_old_default_ref": OLD_PIN}
    invalid = dict(valid)
    invalid["root_id"] = "bad"
    invalid["operation_rows"] = []
    result = wb.preview_specification_replacement_batch(client, [invalid, valid])
    assert result["summary"] == {"total": 2, "preview_ready": 1, "blocked": 1}
    assert [row["status"] for row in result["roots"]] == ["blocked", "preview_ready"]
    assert client.posts == [] and client.patches == []


def test_compensation_preflights_all_entries_and_aborts_on_concurrent_change():
    after_rows = [_guarded_row("1", ITEM_A, wb.ZERO_GUID, "Сборка", TARGET_A)]
    before_rows = [_guarded_row("1", ITEM_A, wb.ZERO_GUID, "Сборка", OLD_PIN)]
    defaults = [
        {
            "Номенклатура_Key": ITEM_A,
            "Характеристика_Key": wb.ZERO_GUID,
            "Спецификация_Key": TARGET_A,
            "DataVersion": "d2",
            "@odata.etag": 'W/"d2"',
        }
    ]
    client = _GuardedClient(
        {PARENT_SPEC: _full_spec(PARENT_SPEC, PARENT_OWNER, after_rows, version="changed")},
        defaults,
    )
    journal = [
        {
            "kind": "parent_composition",
            "spec_ref": PARENT_SPEC,
            "before_sostav": before_rows,
            "after_sostav": after_rows,
            "after_data_version": "v2",
            "after_etag": 'W/"v2"',
            "concurrency_runtime_validated": True,
        },
        {
            "kind": "default_specification",
            "nomenclature_key": ITEM_A,
            "characteristic_key": wb.ZERO_GUID,
            "before_spec_ref": OLD_PIN,
            "after_spec_ref": TARGET_A,
            "after_data_version": "d2",
            "after_etag": 'W/"d2"',
            "concurrency_runtime_validated": True,
        },
    ]
    with pytest.raises(wb.SpecWritebackError, match="DataVersion drift"):
        wb.compensate_specification_writeback(
            client, journal=journal, mutation_mode=wb.MUTATION_APPLY_GUARDED
        )
    assert client.patches == []


def test_compensation_restores_default_and_parent_with_after_etags_only():
    after_rows = [_guarded_row("1", ITEM_A, wb.ZERO_GUID, "Сборка", TARGET_A)]
    before_rows = [_guarded_row("1", ITEM_A, wb.ZERO_GUID, "Сборка", OLD_PIN)]
    defaults = [
        {
            "Номенклатура_Key": ITEM_A,
            "Характеристика_Key": wb.ZERO_GUID,
            "Спецификация_Key": TARGET_A,
            "DataVersion": "d2",
            "@odata.etag": 'W/"d2"',
        }
    ]
    client = _GuardedClient(
        {
            PARENT_SPEC: _full_spec(
                PARENT_SPEC,
                PARENT_OWNER,
                after_rows,
                version="v2",
                etag='W/"v2"',
            )
        },
        defaults,
    )
    journal = [
        {
            "kind": "parent_composition",
            "spec_ref": PARENT_SPEC,
            "before_sostav": before_rows,
            "after_sostav": after_rows,
            "after_data_version": "v2",
            "after_etag": 'W/"v2"',
            "concurrency_runtime_validated": True,
        },
        {
            "kind": "default_specification",
            "nomenclature_key": ITEM_A,
            "characteristic_key": wb.ZERO_GUID,
            "before_spec_ref": OLD_PIN,
            "after_spec_ref": TARGET_A,
            "after_data_version": "d2",
            "after_etag": 'W/"d2"',
            "concurrency_runtime_validated": True,
        },
    ]
    preview = wb.compensate_specification_writeback(client, journal=journal)
    assert preview["status"] == "preview" and preview["apply_capable"] is True
    assert client.patches == []
    applied = wb.compensate_specification_writeback(
        client, journal=journal, mutation_mode=wb.MUTATION_APPLY_GUARDED
    )
    assert applied == {
        "status": "compensated",
        "mutation_mode": wb.MUTATION_APPLY_GUARDED,
        "steps": 2,
        "delete_specifications": [],
    }
    assert client.defaults[0]["Спецификация_Key"] == OLD_PIN
    assert client.specs[PARENT_SPEC]["Состав"] == before_rows
    assert [patch[2] for patch in client.patches] == ['W/"d2"', 'W/"v2"']
