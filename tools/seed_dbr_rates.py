#!/usr/bin/env python
"""Сидер справочников DBR-контура: такты сборки и мощность участков.

Приёмка поколения падает, если в контуре барабана нет опорных данных
(``backend/app/services/item_ledger/drum_scheduler.py``):

* ``missing assembly rate for item ...`` — для позиции очереди нет строки
  ``dbr_assembly_rate``;
* ``ambiguous assembly rates for item ...`` — тактов больше одного
  (позиция висит на двух участках);
* ``non-positive capacity for resource ...`` — у участка, на который указывает
  такт, мощность 0.

Скрипт заполняет ровно эти три дырки и только через HTTP API
(``backend/app/routers/planning_rates.py``): никаких прямых подключений к БД.

Что делает:

1. читает TSV со связкой «участок → код номенклатуры → такт»;
2. резолвит имена участков через ``GET /v1/planning-rates/resources``
   (регистр и пробелы не важны), коды номенклатуры — через поиск
   ``GET /v1/nomenclature/search`` либо постранично через ``GET /v1/items/``;
3. заливает такты через ``PUT /v1/planning-rates/assembly-rates``
   (bulk-upsert по паре ``(resource_id, item_id)`` — повторный прогон
   не плодит дублей);
4. по ``--capacity N`` чинит нулевую мощность через
   ``PATCH /v1/planning-rates/resources/{id}`` — только тем участкам, которые
   реально встречаются в тактах (барабан проверяет мощность лишь у ресурса,
   на который указывает такт позиции), и только там, где мощность 0.

Нерезолвленные имена и коды не роняют прогон: они попадают в отчёт, остальное
заливается. Код возврата: 0 — всё сошлось, 1 — часть строк не сошлась
(или найден риск неоднозначности), 2 — транспортная/протокольная ошибка.

Примеры:

    python tools/seed_dbr_rates.py --base-url http://127.0.0.1:8020/api --dry-run
    python tools/seed_dbr_rates.py --base-url http://127.0.0.1:8020/api --capacity 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


DEFAULT_TSV = Path(".docs/dbr_seed/erpnext_assembly_rates.tsv")
DEFAULT_BASE_URL = "http://127.0.0.1:8020/api"
DEFAULT_BATCH_SIZE = 200
DEFAULT_TIMEOUT = 60.0
TOKEN_ENV = "PRODPLAN_API_TOKEN"

RESOURCE_HEADERS = ("workstation", "resource", "resource_name", "участок", "ресурс")
ITEM_HEADERS = ("item", "item_code", "code", "номенклатура", "код")
QTY_HEADERS = ("qty_per_capacity", "qty", "rate", "такт", "норма")


# ---------------------------------------------------------------------------
# Нормализация и разбор TSV (чистые функции, тестируются без сети)
# ---------------------------------------------------------------------------


_WS_RE = re.compile(r"\s+", re.UNICODE)


def normalize_name(value: Any) -> str:
    """Ключ сравнения имён участков: регистр, ё/е и пробелы не значимы."""
    text = "" if value is None else str(value)
    text = text.replace(" ", " ").replace(" ", " ")
    text = _WS_RE.sub(" ", text).strip()
    return text.casefold().replace("ё", "е")


def normalize_code(value: Any) -> str:
    """Ключ сравнения кодов номенклатуры: регистр и лишние пробелы не значимы."""
    text = "" if value is None else str(value)
    text = text.replace(" ", " ").replace(" ", " ")
    return _WS_RE.sub(" ", text).strip().casefold()


def parse_decimal(value: Any) -> Optional[Decimal]:
    """Такт из TSV: точка или запятая, отрицательные и нули отбрасываются."""
    text = "" if value is None else str(value).strip().replace(" ", "").replace(" ", "")
    if not text:
        return None
    text = text.replace(",", ".")
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


@dataclass(frozen=True)
class SeedRate:
    """Одна строка сида: участок, код номенклатуры, такт."""

    line_no: int
    workstation: str
    item_code: str
    qty_per_capacity: Decimal

    @property
    def resource_key(self) -> str:
        return normalize_name(self.workstation)

    @property
    def item_key(self) -> str:
        return normalize_code(self.item_code)


@dataclass
class ParsedTsv:
    rows: list[SeedRate] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    skipped_duplicates: list[str] = field(default_factory=list)


def _resolve_columns(header: Sequence[str]) -> Optional[tuple[int, int, int]]:
    lowered = [normalize_code(cell) for cell in header]

    def find(candidates: Iterable[str]) -> Optional[int]:
        for candidate in candidates:
            key = normalize_code(candidate)
            for index, cell in enumerate(lowered):
                if cell == key:
                    return index
        return None

    resource_idx = find(RESOURCE_HEADERS)
    item_idx = find(ITEM_HEADERS)
    qty_idx = find(QTY_HEADERS)
    if resource_idx is None or item_idx is None or qty_idx is None:
        return None
    return resource_idx, item_idx, qty_idx


def parse_rates_tsv(text: str) -> ParsedTsv:
    """Разобрать TSV «участок / код номенклатуры / такт».

    Заголовок опознаётся по именам колонок (в т.ч. русским); если его нет —
    читаются первые три колонки позиционно. Битые строки не роняют разбор,
    а попадают в ``problems``. Повтор пары (участок, код) схлопывается:
    последняя строка выигрывает, потому что bulk-upsert отвергает дубли
    ``(resource_id, item_id)`` в одном запросе.
    """
    parsed = ParsedTsv()
    lines = text.splitlines()
    if not lines:
        return parsed

    columns = _resolve_columns(lines[0].split("\t"))
    if columns is None:
        columns = (0, 1, 2)
        start = 0
    else:
        start = 1
    resource_idx, item_idx, qty_idx = columns
    width = max(columns) + 1

    by_pair: dict[tuple[str, str], SeedRate] = {}
    for offset, raw in enumerate(lines[start:], start=start + 1):
        if not raw.strip():
            continue
        cells = raw.split("\t")
        if len(cells) < width:
            parsed.problems.append(f"строка {offset}: ожидалось {width} колонок, получено {len(cells)}")
            continue
        workstation = cells[resource_idx].strip()
        item_code = cells[item_idx].strip()
        qty = parse_decimal(cells[qty_idx])
        if not workstation:
            parsed.problems.append(f"строка {offset}: пустое имя участка")
            continue
        if not item_code:
            parsed.problems.append(f"строка {offset}: пустой код номенклатуры")
            continue
        if qty is None:
            parsed.problems.append(
                f"строка {offset}: некорректный такт {cells[qty_idx]!r} (нужно положительное число)"
            )
            continue
        row = SeedRate(
            line_no=offset,
            workstation=workstation,
            item_code=item_code,
            qty_per_capacity=qty,
        )
        key = (row.resource_key, row.item_key)
        previous = by_pair.get(key)
        if previous is not None:
            parsed.skipped_duplicates.append(
                f"{row.workstation} / {row.item_code}: строка {previous.line_no} перекрыта строкой {offset}"
            )
        by_pair[key] = row

    parsed.rows = sorted(by_pair.values(), key=lambda row: row.line_no)
    return parsed


# ---------------------------------------------------------------------------
# Матчинг
# ---------------------------------------------------------------------------


@dataclass
class MatchOutcome:
    payload: list[dict[str, Any]] = field(default_factory=list)
    matched_rows: list[SeedRate] = field(default_factory=list)
    resource_ids: dict[int, str] = field(default_factory=dict)
    unmatched_resources: dict[str, int] = field(default_factory=dict)
    ambiguous_resources: dict[str, int] = field(default_factory=dict)
    unmatched_items: list[str] = field(default_factory=list)
    conflicting_items: dict[str, list[str]] = field(default_factory=dict)


def index_resources(resources: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Имя участка (нормализованное) → строки ресурса; имя в БД не уникально."""
    index: dict[str, list[dict[str, Any]]] = {}
    for row in resources:
        key = normalize_name(row.get("resource_name"))
        if not key:
            continue
        index.setdefault(key, []).append(row)
    return index


def match_rates(
    rows: Sequence[SeedRate],
    resources: Sequence[dict[str, Any]],
    item_ids_by_code: dict[str, int],
) -> MatchOutcome:
    """Свести строки сида к payload bulk-upsert; несведённое — в отчёт."""
    outcome = MatchOutcome()
    resource_index = index_resources(resources)

    # Один и тот же item на двух участках сделает такт неоднозначным и уронит
    # приёмку — ловим это до записи.
    resources_by_item: dict[str, set[str]] = {}
    for row in rows:
        resources_by_item.setdefault(row.item_key, set()).add(row.workstation.strip())
    conflicting_keys = {key for key, names in resources_by_item.items() if len(names) > 1}
    for row in rows:
        if row.item_key in conflicting_keys:
            outcome.conflicting_items[row.item_code] = sorted(resources_by_item[row.item_key])

    for row in rows:
        candidates = resource_index.get(row.resource_key, [])
        if not candidates:
            outcome.unmatched_resources[row.workstation] = (
                outcome.unmatched_resources.get(row.workstation, 0) + 1
            )
            continue
        if len(candidates) > 1:
            outcome.ambiguous_resources[row.workstation] = len(candidates)
            continue
        if row.item_key in conflicting_keys:
            continue
        item_id = item_ids_by_code.get(row.item_key)
        if item_id is None:
            outcome.unmatched_items.append(row.item_code)
            continue
        resource_id = int(candidates[0]["resource_id"])
        outcome.resource_ids[resource_id] = str(candidates[0].get("resource_name") or "")
        outcome.matched_rows.append(row)
        outcome.payload.append(
            {
                "item_id": int(item_id),
                "resource_id": resource_id,
                "qty_per_capacity": format(row.qty_per_capacity.normalize(), "f"),
            }
        )
    return outcome


def capacity_targets(
    resources: Sequence[dict[str, Any]],
    resource_ids: Iterable[int],
) -> list[dict[str, Any]]:
    """Участки из тактов с нулевой мощностью — минимально необходимый патч.

    Барабан делит на мощность только того ресурса, на который указывает такт
    позиции очереди, поэтому трогать остальные участки незачем.
    """
    wanted = {int(value) for value in resource_ids}
    targets = []
    for row in resources:
        resource_id = int(row["resource_id"])
        if resource_id not in wanted:
            continue
        if float(row.get("capacity") or 0) > 0:
            continue
        targets.append(row)
    return targets


def ambiguity_risks(
    existing_rates: Sequence[dict[str, Any]],
    payload: Sequence[dict[str, Any]],
) -> list[str]:
    """Уже лежащие в БД такты того же item на другом участке.

    Upsert их не перезапишет (ключ — пара resource+item), а приёмка потом
    упадёт на ``ambiguous assembly rates``.
    """
    planned: dict[int, int] = {int(row["item_id"]): int(row["resource_id"]) for row in payload}
    risks: list[str] = []
    for row in existing_rates:
        item_id = int(row.get("item_id", 0))
        resource_id = int(row.get("resource_id", 0))
        target = planned.get(item_id)
        if target is None or target == resource_id:
            continue
        risks.append(
            f"item {item_id} ({row.get('item_code') or '?'}): в БД такт на ресурсе "
            f"{resource_id} ({row.get('resource_name') or '?'}), сид ставит на {target}"
        )
    return risks


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    pass


class HttpClient:
    """Тонкий клиент поверх urllib: без внешних зависимостей, как в tools/diagnostics."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        data = None
        request = urllib.request.Request(url, method=method.upper())
        request.add_header("Accept", "application/json")
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", self.token)
        try:
            with urllib.request.urlopen(request, data=data, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - сеть
            detail = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"{method} {url} -> HTTP {exc.code}: {detail[:2000]}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - сеть
            raise ApiError(f"{method} {url} -> недоступно: {exc.reason}") from exc
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:  # pragma: no cover - сеть
            raise ApiError(f"{method} {url} -> ответ не JSON: {payload[:500]}") from exc


class SeedApi:
    """Доменные вызовы сида. Тест подменяет этот объект фейком."""

    def __init__(self, client: HttpClient, *, page_size: int = 500):
        self.client = client
        self.page_size = page_size

    def list_resources(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.client.request(
                "GET",
                "/v1/planning-rates/resources",
                params={"limit": min(self.page_size, 1000), "offset": offset},
            )
            chunk = list(page.get("rows") or [])
            rows.extend(chunk)
            total = int(page.get("total") or 0)
            if not chunk or len(rows) >= total:
                break
            offset += len(chunk)
        return rows

    def list_assembly_rates(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.client.request(
                "GET",
                "/v1/planning-rates/assembly-rates",
                params={"limit": min(self.page_size, 1000), "offset": offset},
            )
            chunk = list(page.get("rows") or [])
            rows.extend(chunk)
            total = int(page.get("total") or 0)
            if not chunk or len(rows) >= total:
                break
            offset += len(chunk)
        return rows

    def search_items(self, code: str) -> list[dict[str, Any]]:
        page = self.client.request(
            "GET",
            "/v1/nomenclature/search",
            params={"q": code, "limit": 100},
        )
        return list(page.get("items") or [])

    def iter_items(self) -> Iterable[dict[str, Any]]:
        """Постраничный обход всей номенклатуры (``skip``/``limit``)."""
        skip = 0
        seen = 0
        while True:
            page = self.client.request(
                "GET",
                "/v1/items/",
                params={"skip": skip, "limit": self.page_size},
            )
            chunk = list(page.get("rows") or [])
            if not chunk:
                return
            for row in chunk:
                yield row
            seen += len(chunk)
            total = int(page.get("total") or 0)
            if total and seen >= total:
                return
            skip += len(chunk)

    def upsert_rates(self, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        return self.client.request(
            "PUT",
            "/v1/planning-rates/assembly-rates",
            body={"rows": list(rows)},
        )

    def patch_capacity(self, resource_id: int, capacity: Decimal) -> dict[str, Any]:
        return self.client.request(
            "PATCH",
            f"/v1/planning-rates/resources/{int(resource_id)}",
            body={"capacity": format(Decimal(capacity), "f")},
        )


def resolve_items_by_search(api: SeedApi, codes: Sequence[str]) -> dict[str, int]:
    """Точное совпадение кода среди результатов подстрочного поиска."""
    resolved: dict[str, int] = {}
    for code in codes:
        key = normalize_code(code)
        if key in resolved:
            continue
        matches = [
            row
            for row in api.search_items(code)
            if normalize_code(row.get("item_code")) == key
        ]
        if len(matches) == 1:
            resolved[key] = int(matches[0]["item_id"])
    return resolved


def resolve_items_by_list(api: SeedApi, codes: Sequence[str]) -> dict[str, int]:
    """Полный постраничный проход по номенклатуре (когда поиск недоступен)."""
    wanted = {normalize_code(code) for code in codes}
    resolved: dict[str, int] = {}
    for row in api.iter_items():
        key = normalize_code(row.get("item_code"))
        if key in wanted and key not in resolved:
            resolved[key] = int(row["item_id"])
            if len(resolved) == len(wanted):
                break
    return resolved


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------


@dataclass
class SeedReport:
    parsed: ParsedTsv
    outcome: MatchOutcome
    created: int = 0
    updated: int = 0
    capacity_patched: list[str] = field(default_factory=list)
    capacity_kept: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def has_gaps(self) -> bool:
        return bool(
            self.parsed.problems
            or self.outcome.unmatched_resources
            or self.outcome.ambiguous_resources
            or self.outcome.unmatched_items
            or self.outcome.conflicting_items
            or self.risks
        )


def run_seed(
    api: SeedApi,
    parsed: ParsedTsv,
    *,
    items_source: str = "search",
    capacity: Optional[Decimal] = None,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> SeedReport:
    resources = api.list_resources()
    codes = [row.item_code for row in parsed.rows]
    if items_source == "list":
        item_ids = resolve_items_by_list(api, codes)
    else:
        item_ids = resolve_items_by_search(api, codes)

    outcome = match_rates(parsed.rows, resources, item_ids)
    report = SeedReport(parsed=parsed, outcome=outcome, dry_run=dry_run)

    if outcome.payload:
        report.risks = ambiguity_risks(api.list_assembly_rates(), outcome.payload)

    if outcome.payload and not dry_run:
        for start in range(0, len(outcome.payload), max(1, batch_size)):
            chunk = outcome.payload[start : start + max(1, batch_size)]
            response = api.upsert_rates(chunk) or {}
            report.created += int(response.get("created") or 0)
            report.updated += int(response.get("updated") or 0)

    if capacity is not None:
        targets = capacity_targets(resources, outcome.resource_ids)
        for row in targets:
            label = f"{row['resource_id']} ({row.get('resource_name')}): 0 -> {format(capacity, 'f')}"
            if not dry_run:
                api.patch_capacity(int(row["resource_id"]), capacity)
            report.capacity_patched.append(label)
        target_ids = {int(row["resource_id"]) for row in targets}
        for row in resources:
            resource_id = int(row["resource_id"])
            if resource_id in outcome.resource_ids and resource_id not in target_ids:
                report.capacity_kept.append(
                    f"{resource_id} ({row.get('resource_name')}): {row.get('capacity')}"
                )
    return report


def format_report(report: SeedReport) -> str:
    outcome = report.outcome
    lines: list[str] = []
    mode = "DRY-RUN (ничего не записано)" if report.dry_run else "ЗАПИСЬ"
    lines.append(f"Режим: {mode}")
    lines.append(f"Строк в TSV принято: {len(report.parsed.rows)}")
    lines.append(f"Тактов сведено: {len(outcome.payload)}")
    if report.dry_run:
        lines.append("Такты: было бы отправлено в bulk-upsert " f"{len(outcome.payload)} строк")
    else:
        lines.append(f"Такты: created={report.created}, updated={report.updated}")

    if report.capacity_patched:
        lines.append("Мощность выправлена:")
        lines.extend(f"  - {row}" for row in report.capacity_patched)
    if report.capacity_kept:
        lines.append("Мощность оставлена как есть (ненулевая):")
        lines.extend(f"  - {row}" for row in report.capacity_kept)

    if report.parsed.problems:
        lines.append("Битые строки TSV:")
        lines.extend(f"  - {row}" for row in report.parsed.problems)
    if report.parsed.skipped_duplicates:
        lines.append("Дубли пары (участок, код) схлопнуты:")
        lines.extend(f"  - {row}" for row in report.parsed.skipped_duplicates)
    if outcome.unmatched_resources:
        lines.append("Участки не найдены в /planning-rates/resources:")
        lines.extend(
            f"  - {name} (строк: {count})"
            for name, count in sorted(outcome.unmatched_resources.items())
        )
    if outcome.ambiguous_resources:
        lines.append("Имя участка неоднозначно (несколько ресурсов с таким именем):")
        lines.extend(
            f"  - {name} (кандидатов: {count})"
            for name, count in sorted(outcome.ambiguous_resources.items())
        )
    if outcome.unmatched_items:
        lines.append("Коды номенклатуры не найдены:")
        lines.extend(f"  - {code}" for code in sorted(set(outcome.unmatched_items)))
    if outcome.conflicting_items:
        lines.append("Позиция в TSV висит на нескольких участках (такт был бы неоднозначным, строки пропущены):")
        lines.extend(
            f"  - {code}: {', '.join(names)}"
            for code, names in sorted(outcome.conflicting_items.items())
        )
    if report.risks:
        lines.append("Риск неоднозначности: в БД уже есть такт этой позиции на другом участке:")
        lines.extend(f"  - {row}" for row in report.risks)

    if not report.has_gaps:
        lines.append("Расхождений нет.")
    return "\n".join(lines)


def read_tsv(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _decimal_arg(value: str) -> Decimal:
    parsed = parse_decimal(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"ожидалось положительное число, получено {value!r}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Заливка тактов сборки и мощности участков через HTTP API PRODPLAN",
    )
    parser.add_argument("--tsv", type=Path, default=DEFAULT_TSV, help=f"путь к TSV (по умолчанию {DEFAULT_TSV})")
    parser.add_argument(
        "--base-url",
        default=os.getenv("PRODPLAN_API_BASE_URL", DEFAULT_BASE_URL),
        help=f"базовый URL API (по умолчанию {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--capacity",
        type=_decimal_arg,
        default=None,
        help="выставить эту мощность участкам из тактов, у которых сейчас 0",
    )
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    parser.add_argument(
        "--items-source",
        choices=("search", "list"),
        default="search",
        help="как искать item_id по коду: search (по одному запросу на код) или list (полный обход /v1/items)",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="размер пачки для bulk-upsert")
    parser.add_argument("--page-size", type=int, default=500, help="размер страницы при обходе списков")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="таймаут HTTP, сек")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    tsv_path = args.tsv if args.tsv.is_absolute() else Path.cwd() / args.tsv
    if not tsv_path.exists():
        print(f"TSV не найден: {tsv_path}", file=sys.stderr)
        return 2
    if args.capacity is not None and args.capacity <= 0:
        print("--capacity должен быть положительным", file=sys.stderr)
        return 2

    parsed = parse_rates_tsv(read_tsv(tsv_path))
    if not parsed.rows:
        print(f"В {tsv_path} нет пригодных строк", file=sys.stderr)
        for problem in parsed.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    client = HttpClient(args.base_url, timeout=args.timeout, token=os.getenv(TOKEN_ENV))
    api = SeedApi(client, page_size=max(1, args.page_size))
    try:
        report = run_seed(
            api,
            parsed,
            items_source=args.items_source,
            capacity=args.capacity,
            dry_run=bool(args.dry_run),
            batch_size=max(1, args.batch_size),
        )
    except ApiError as exc:
        print(f"Ошибка API: {exc}", file=sys.stderr)
        return 2

    print(f"Источник: {tsv_path}")
    print(f"API: {client.base_url}")
    print(format_report(report))
    return 1 if report.has_gaps else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
