"""Сидер тактов сборки: разбор TSV и матчинг имён без сети.

Скрипт живёт вне ``backend/app``, поэтому здесь проверяется только его чистая
часть плюс прогон через фейковый клиент API.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tools.seed_dbr_rates import (
    SeedApi,
    ambiguity_risks,
    capacity_targets,
    format_report,
    match_rates,
    normalize_code,
    normalize_name,
    parse_rates_tsv,
    run_seed,
)


HEADER = "workstation\titem\tqty_per_capacity\n"


class FakeApi(SeedApi):
    """Подмена SeedApi: те же методы, никакого HTTP."""

    def __init__(self, resources, items_by_code, existing_rates=None):
        self._resources = [dict(row) for row in resources]
        self._items = dict(items_by_code)
        self._existing = list(existing_rates or [])
        self.upserted: list[list[dict]] = []
        self.patched: list[tuple[int, Decimal]] = []

    def list_resources(self):
        return [dict(row) for row in self._resources]

    def list_assembly_rates(self):
        return list(self._existing)

    def search_items(self, code):
        found = self._items.get(normalize_code(code))
        rows = [{"item_id": found, "item_code": code}] if found else []
        # Подстрочный поиск возвращает и «соседей» — сидер обязан их отсеять.
        rows.append({"item_id": 999_000, "item_code": f"{code}-СТАРЫЙ"})
        return rows

    def iter_items(self):
        for code, item_id in self._items.items():
            yield {"item_id": item_id, "item_code": code}

    def upsert_rates(self, rows):
        self.upserted.append([dict(row) for row in rows])
        return {"rows": [], "created": len(rows), "updated": 0}

    def patch_capacity(self, resource_id, capacity):
        self.patched.append((int(resource_id), Decimal(capacity)))
        for row in self._resources:
            if int(row["resource_id"]) == int(resource_id):
                row["capacity"] = float(capacity)
        return dict(row)


def _resources():
    return [
        {"resource_id": 1, "resource_name": "Участок сборки снегоходов", "capacity": 0.0},
        {"resource_id": 2, "resource_name": "Участок сборки мотобуксировщиков", "capacity": 5.0},
        {"resource_id": 3, "resource_name": "Малярка", "capacity": 0.0},
    ]


# ---------------------------------------------------------------------------
# Разбор TSV
# ---------------------------------------------------------------------------


def test_parses_header_and_decimal_takt():
    parsed = parse_rates_tsv(
        HEADER
        + "Участок сборки снегоходов\tНФ-00009114\t1.000000000\n"
        + "Участок сборки мотобуксировщиков\t00-00001514\t3.000000000\n"
    )
    assert parsed.problems == []
    assert [row.item_code for row in parsed.rows] == ["НФ-00009114", "00-00001514"]
    assert parsed.rows[1].qty_per_capacity == Decimal("3")


def test_parses_without_header_positionally():
    parsed = parse_rates_tsv("Участок сборки снегоходов\tНФ-1\t2\n")
    assert len(parsed.rows) == 1
    assert parsed.rows[0].qty_per_capacity == Decimal("2")


def test_broken_rows_are_reported_not_fatal():
    parsed = parse_rates_tsv(
        HEADER
        + "Участок сборки снегоходов\tНФ-1\t1\n"
        + "\tНФ-2\t1\n"
        + "Участок сборки снегоходов\t\t1\n"
        + "Участок сборки снегоходов\tНФ-3\t0\n"
        + "Участок сборки снегоходов\tНФ-4\tабв\n"
        + "мало колонок\n"
    )
    assert [row.item_code for row in parsed.rows] == ["НФ-1"]
    assert len(parsed.problems) == 5


def test_duplicate_pair_collapses_last_wins():
    parsed = parse_rates_tsv(
        HEADER
        + "Участок сборки снегоходов\tНФ-1\t1\n"
        + "участок  СБОРКИ снегоходов\tнф-1\t7\n"
    )
    assert len(parsed.rows) == 1
    assert parsed.rows[0].qty_per_capacity == Decimal("7")
    assert parsed.skipped_duplicates


def test_comma_decimal_and_spaces():
    parsed = parse_rates_tsv(HEADER + "  Участок сборки снегоходов \t НФ-1 \t 2,5 \n")
    assert parsed.rows[0].qty_per_capacity == Decimal("2.5")
    assert parsed.rows[0].workstation == "Участок сборки снегоходов"


# ---------------------------------------------------------------------------
# Нормализация и матчинг
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right",
    [
        ("Участок сборки снегоходов", "  участок   СБОРКИ  снегоходов "),
        ("Участок сборки модулей", "Участок\tсборки\tмодулей"),
        ("Ёлка", "елка"),
    ],
)
def test_resource_names_match_case_and_space_insensitively(left, right):
    assert normalize_name(left) == normalize_name(right)


def test_match_resolves_ids_and_reports_gaps():
    parsed = parse_rates_tsv(
        HEADER
        + "участок сборки СНЕГОХОДОВ\tНФ-1\t1\n"
        + "  Участок сборки мотобуксировщиков  \tНФ-2\t3\n"
        + "Участок сборки вертолётов\tНФ-3\t1\n"
        + "Участок сборки снегоходов\tНФ-НЕТ\t1\n"
    )
    outcome = match_rates(parsed.rows, _resources(), {"нф-1": 11, "нф-2": 22, "нф-3": 33})

    assert outcome.payload == [
        {"item_id": 11, "resource_id": 1, "qty_per_capacity": "1"},
        {"item_id": 22, "resource_id": 2, "qty_per_capacity": "3"},
    ]
    assert outcome.unmatched_resources == {"Участок сборки вертолётов": 1}
    assert outcome.unmatched_items == ["НФ-НЕТ"]
    assert set(outcome.resource_ids) == {1, 2}


def test_ambiguous_resource_name_is_skipped_not_guessed():
    resources = _resources() + [
        {"resource_id": 4, "resource_name": "участок сборки снегоходов", "capacity": 1.0}
    ]
    parsed = parse_rates_tsv(HEADER + "Участок сборки снегоходов\tНФ-1\t1\n")
    outcome = match_rates(parsed.rows, resources, {"нф-1": 11})
    assert outcome.payload == []
    assert outcome.ambiguous_resources == {"Участок сборки снегоходов": 2}


def test_item_on_two_workstations_is_refused():
    parsed = parse_rates_tsv(
        HEADER
        + "Участок сборки снегоходов\tНФ-1\t1\n"
        + "Участок сборки мотобуксировщиков\tНФ-1\t3\n"
    )
    outcome = match_rates(parsed.rows, _resources(), {"нф-1": 11})
    assert outcome.payload == []
    assert outcome.conflicting_items["НФ-1"] == [
        "Участок сборки мотобуксировщиков",
        "Участок сборки снегоходов",
    ]


def test_existing_rate_on_other_resource_is_flagged():
    payload = [{"item_id": 11, "resource_id": 1, "qty_per_capacity": "1"}]
    existing = [
        {"item_id": 11, "resource_id": 2, "item_code": "НФ-1", "resource_name": "Мотобуксировщики"},
        {"item_id": 11, "resource_id": 1, "item_code": "НФ-1", "resource_name": "Снегоходы"},
    ]
    risks = ambiguity_risks(existing, payload)
    assert len(risks) == 1
    assert "НФ-1" in risks[0]


# ---------------------------------------------------------------------------
# Мощность
# ---------------------------------------------------------------------------


def test_capacity_targets_only_zero_and_only_referenced():
    targets = capacity_targets(_resources(), {1, 2})
    assert [row["resource_id"] for row in targets] == [1]


# ---------------------------------------------------------------------------
# Прогон целиком через фейковый клиент
# ---------------------------------------------------------------------------


def _tsv():
    return (
        HEADER
        + "участок сборки снегоходов\tНФ-1\t1.000000000\n"
        + "Участок сборки мотобуксировщиков\tНФ-2\t3.000000000\n"
        + "Участок сборки вертолётов\tНФ-3\t1\n"
    )


def test_run_seed_writes_rates_and_patches_only_zero_capacity():
    api = FakeApi(_resources(), {"нф-1": 11, "нф-2": 22, "нф-3": 33})
    report = run_seed(api, parse_rates_tsv(_tsv()), capacity=Decimal("1"))

    assert api.upserted == [
        [
            {"item_id": 11, "resource_id": 1, "qty_per_capacity": "1"},
            {"item_id": 22, "resource_id": 2, "qty_per_capacity": "3"},
        ]
    ]
    assert api.patched == [(1, Decimal("1"))]  # ресурс 2 уже с мощностью, ресурс 3 не в тактах
    assert report.created == 2
    assert "Участок сборки вертолётов" in format_report(report)
    assert report.has_gaps is True


def test_dry_run_touches_nothing():
    api = FakeApi(_resources(), {"нф-1": 11, "нф-2": 22})
    report = run_seed(api, parse_rates_tsv(_tsv()), capacity=Decimal("1"), dry_run=True)
    assert api.upserted == []
    assert api.patched == []
    assert report.capacity_patched == ["1 (Участок сборки снегоходов): 0 -> 1"]
    assert "DRY-RUN" in format_report(report)


def test_items_source_list_walks_pages():
    api = FakeApi(_resources(), {"нф-1": 11, "нф-2": 22})
    report = run_seed(api, parse_rates_tsv(_tsv()), items_source="list")
    assert [row["item_id"] for row in report.outcome.payload] == [11, 22]


def test_second_run_is_idempotent_on_payload():
    api = FakeApi(_resources(), {"нф-1": 11, "нф-2": 22})
    parsed = parse_rates_tsv(_tsv())
    first = run_seed(api, parsed, capacity=Decimal("1"))
    second = run_seed(api, parsed, capacity=Decimal("1"))
    assert first.outcome.payload == second.outcome.payload
    assert api.patched == [(1, Decimal("1"))]  # второй прогон мощность уже не трогает
    assert len(api.upserted) == 2


def test_batching_splits_payload():
    api = FakeApi(_resources(), {"нф-1": 11, "нф-2": 22})
    run_seed(api, parse_rates_tsv(_tsv()), batch_size=1)
    assert [len(chunk) for chunk in api.upserted] == [1, 1]
