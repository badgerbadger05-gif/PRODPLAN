#!/usr/bin/env python3
"""Перенос оптимальных партий запуска со сварных деталей на родительские окраски.

Контекст: после того как сварку подняли в спецификацию окраски, запуск идёт
со строки окрашенной детали, а ``Item.optimal_batch`` остался записанным на
сварном предшественнике. Скрипт находит пары «окраска ↔ сварка» и прописывает
партию окрашенной позиции, У КОТОРОЙ ОНА ПУСТАЯ. Существующие значения никогда
не перезаписываются.

Источники пар (в порядке приоритета):
  1. Реестр PaintWeldPair (``GET /v1/paint-weld/pairs?active_only=false``) —
     включая деактивированные auto-пары: после изменения спецификаций они
     снялись, но связь «какая сварка чья окраска» остаётся достоверной.
  2. Артикульное соглашение ``<база>-S`` ↔ ``<база>-SP*`` — только если имя
     окрашенной позиции содержит красящий маркер (покрас/окрас/черн/красн/
     син/бел/зел/жёлт/желт); остальные попадают в отчёт ``review``.

Запись идёт только через санкционированный ``PATCH /v1/items/{item_id}``
(тот же endpoint, что и редактор партии в UI). Другие поля не затрагиваются.

Использование:
  python tools/propagate_paint_optimal_batch.py --base-url http://host:9020
  python tools/propagate_paint_optimal_batch.py --base-url http://host:9020 --apply

Без ``--apply`` выполняется dry-run: план печатается и сохраняется в
``--output`` (UTF-8 JSON), ничего не записывается. С ``--apply`` каждая запись
логируется в тот же файл с полями before/after.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

WELDED_ARTICLE_RE = re.compile(r"^(.*)-S$")
PAINTED_ARTICLE_RE = re.compile(r"^(.*)-SP[A-Za-z]*$")
PAINT_NAME_MARKERS = ("покрас", "окрас", "черн", "красн", "син", "бел", "зел", "жёлт", "желт")

ITEMS_PAGE_LIMIT = 500


def _get_json(base_url: str, path: str) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def _patch_json(base_url: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="PATCH",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_all_items(base_url: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    skip = 0
    while True:
        page = _get_json(base_url, f"/api/v1/items/?skip={skip}&limit={ITEMS_PAGE_LIMIT}")
        batch = page.get("rows") or []
        rows.extend(batch)
        total = int(page.get("total") or 0)
        if not batch or len(rows) >= total:
            return rows
        skip += ITEMS_PAGE_LIMIT


def _fetch_pairs(base_url: str) -> List[Dict[str, Any]]:
    payload = _get_json(base_url, "/api/v1/paint-weld/pairs?active_only=false")
    pairs = payload.get("pairs") or payload.get("rows") or []
    if not isinstance(pairs, list):
        raise RuntimeError("unexpected pairs payload shape")
    return pairs


def _batch_of(item: Dict[str, Any]) -> Optional[float]:
    value = item.get("optimal_batch")
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _is_painted_name(name: Optional[str]) -> bool:
    lowered = str(name or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in PAINT_NAME_MARKERS)


def build_plan(
    items: List[Dict[str, Any]],
    pairs: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Вернуть (plan, review): plan — безопасные записи, review — спорные."""
    by_id = {int(row["item_id"]): row for row in items if row.get("item_id") is not None}

    plan: Dict[int, Dict[str, Any]] = {}
    review: List[Dict[str, Any]] = []

    def _consider(painted_id: int, welded_id: int, source: str) -> None:
        painted = by_id.get(painted_id)
        welded = by_id.get(welded_id)
        if painted is None or welded is None:
            return
        if _batch_of(painted) is not None:
            return  # существующее значение не трогаем никогда
        welded_batch = _batch_of(welded)
        if welded_batch is None:
            return
        entry = {
            "painted_item_id": painted_id,
            "painted_article": painted.get("item_article"),
            "painted_name": painted.get("item_name"),
            "welded_item_id": welded_id,
            "welded_article": welded.get("item_article"),
            "welded_name": welded.get("item_name"),
            "optimal_batch": welded_batch,
            "match_source": source,
        }
        if source == "article-pattern" and not _is_painted_name(painted.get("item_name")):
            entry["review_reason"] = "имя без красящего маркера — подтвердить вручную"
            review.append(entry)
            return
        plan.setdefault(painted_id, entry)

    for pair in pairs:
        try:
            painted_id = int(pair["painted_item_id"])
            welded_id = int(pair["welded_item_id"])
        except (KeyError, TypeError, ValueError):
            continue
        source = "pair:active" if pair.get("is_active") else "pair:inactive"
        _consider(painted_id, welded_id, source)

    painted_by_base: Dict[str, List[int]] = {}
    for row in items:
        article = str(row.get("item_article") or "").strip()
        match = PAINTED_ARTICLE_RE.match(article)
        if match and row.get("item_id") is not None:
            painted_by_base.setdefault(match.group(1), []).append(int(row["item_id"]))
    for row in items:
        article = str(row.get("item_article") or "").strip()
        match = WELDED_ARTICLE_RE.match(article)
        if not match or row.get("item_id") is None:
            continue
        if _batch_of(row) is None:
            continue
        for painted_id in painted_by_base.get(match.group(1), []):
            _consider(painted_id, int(row["item_id"]), "article-pattern")

    return [plan[key] for key in sorted(plan)], review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="например http://mtzdock.lan:9020")
    parser.add_argument("--apply", action="store_true", help="выполнить PATCH-запись (иначе dry-run)")
    parser.add_argument("--output", default=None, help="куда сохранить план/журнал (UTF-8 JSON)")
    args = parser.parse_args()

    items = _fetch_all_items(args.base_url)
    pairs = _fetch_pairs(args.base_url)
    plan, review = build_plan(items, pairs)

    report: Dict[str, Any] = {
        "base_url": args.base_url,
        "mode": "apply" if args.apply else "dry-run",
        "planned": len(plan),
        "review": review,
        "entries": [],
    }

    applied = 0
    errors = 0
    for entry in plan:
        record = dict(entry)
        if args.apply:
            try:
                updated = _patch_json(
                    args.base_url,
                    f"/api/v1/items/{entry['painted_item_id']}",
                    {"optimal_batch": entry["optimal_batch"]},
                )
                record["before"] = None
                record["after"] = updated.get("optimal_batch")
                record["applied"] = True
                applied += 1
            except Exception as exc:  # noqa: BLE001 — отчёт должен доехать целиком
                record["applied"] = False
                record["error"] = str(exc)
                errors += 1
        report["entries"].append(record)

    report["applied"] = applied
    report["errors"] = errors

    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
