"""Парсер годового плана выпуска из Google Sheets — чистое ядро.

Формат книги (реальный план владельца, решение 04.07): лист = год,
строка 'Номенклатура | Артикул для пр-ва | <месяц> <нед.1..4(5)> ×12 | ИТОГО'.
Месячная колонка держит итог месяца, недельные — разбивку. Групповые
строки (Снегоходы/МБ/Модули) без артикула — пропускаются, битые ячейки
(#REF!) читаются как 0.

Чистый Python без Frappe: работает и локально (openpyxl → matrix), и в
адаптере импорта (загруженный xlsx). Год заголовков в книге бывает
протухшим (лист копируют из года в год) — поэтому год передаётся
параметром, из ячеек берётся только номер месяца.


Портировано из prodflow prodflow/services/feeder/plan_import.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import NamedTuple

# Русские префиксы месяцев для строковых заголовков («янв.-26», «мая-25»).
_RU_MONTH_PREFIXES = {
	"янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6,
	"июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


class PlanLine(NamedTuple):
	article: str
	name: str
	qty_by_month: dict[str, float]  # "2026-07" → шт


def _month_of(cell) -> int | None:
	if isinstance(cell, (datetime, date)):
		return cell.month
	if isinstance(cell, str):
		text = cell.strip().lower()
		# «1», «2»… — недельные колонки, не месяцы.
		if not text or text.replace(".", "").replace(",", "").isdigit():
			return None
		for prefix, month in _RU_MONTH_PREFIXES.items():
			if text.startswith(prefix):
				return month
	return None


def _qty_of(cell) -> float:
	if cell is None:
		return 0.0
	if isinstance(cell, (int, float)):
		return float(cell)
	text = str(cell).strip().replace("\xa0", "").replace(" ", "")
	if not text or text.startswith("#"):  # #REF! и прочие ошибки формул
		return 0.0
	try:
		return float(text.replace(",", "."))
	except ValueError:
		return 0.0


def parse_plan_matrix(rows: Sequence[Sequence], year: int) -> list[PlanLine]:
	"""Матрица листа → строки плана с помесячными итогами.

	Заголовок ищется по ячейке «Номенклатура»; колонки месяцев — по
	datetime-ячейкам или русским именам месяцев в заголовке. Дубликаты
	артикула суммируются. Строки без артикула (группы, пустые) — мимо.
	"""
	header_idx = name_col = article_col = None
	for i, row in enumerate(rows):
		for j, cell in enumerate(row):
			if isinstance(cell, str) and cell.strip().lower() == "номенклатура":
				header_idx, name_col = i, j
				break
		if header_idx is not None:
			break
	if header_idx is None:
		raise ValueError("Заголовок плана не найден: нет ячейки «Номенклатура»")

	header = rows[header_idx]
	for j, cell in enumerate(header):
		if isinstance(cell, str) and "артикул" in cell.strip().lower():
			article_col = j
			break
	if article_col is None:
		raise ValueError("Заголовок плана не найден: нет колонки «Артикул»")

	month_cols: list[tuple[int, str]] = []
	for j, cell in enumerate(header):
		month = _month_of(cell)
		if month:
			month_cols.append((j, f"{year:04d}-{month:02d}"))
	if not month_cols:
		raise ValueError("В заголовке плана не найдено ни одной колонки месяца")

	acc: dict[str, PlanLine] = {}
	for row in rows[header_idx + 1 :]:
		if len(row) <= article_col:
			continue
		article = row[article_col]
		if not isinstance(article, str) or not article.strip():
			continue
		article = article.strip()
		name = str(row[name_col] or "").strip() if len(row) > name_col else ""
		qty_by_month = {
			month: _qty_of(row[col]) if len(row) > col else 0.0 for col, month in month_cols
		}
		if article in acc:
			prev = acc[article]
			merged = {m: prev.qty_by_month.get(m, 0.0) + q for m, q in qty_by_month.items()}
			acc[article] = PlanLine(article, prev.name or name, merged)
		else:
			acc[article] = PlanLine(article, name, qty_by_month)

	return [acc[a] for a in sorted(acc)]


def period_totals(lines: Sequence[PlanLine], months: Sequence[str]) -> dict[str, float]:
	"""Суммарный объём по артикулу за месяцы периода (для квартального микса)."""
	wanted = set(months)
	totals = {
		line.article: sum(q for m, q in line.qty_by_month.items() if m in wanted)
		for line in lines
	}
	return {a: q for a, q in totals.items() if q > 0}
