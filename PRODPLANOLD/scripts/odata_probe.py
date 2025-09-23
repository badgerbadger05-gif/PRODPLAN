#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Вспомогательный инструмент для безопасной проверки доступности и структуры OData-сущностей 1С.

Функционал:
- читает параметры подключения из config/odata_config.json
- выполняет GET на наборы (EntitySet) с $top=5 по ключевым сущностям из docs/сущности_1С.md
- сохраняет образцы ответов в output/odata_sample_*.json (по одному файлу на сущность)
- формирует сводный output/odata_probe_summary.json:
    * entity: имя EntitySet
    * count: количество записей в образце
    * fields: набор полей (ключи верхнего уровня)
    * expected_fields: ожидаемые поля из документации (если доступны)
    * missing_fields: ожидаемые, но отсутствующие в ответе
    * extra_fields: присутствующие в ответе, но отсутствующие в документации
    * error: текст ошибки (если запрос не удался)
- печатает краткую сводку в stdout

Правила проекта:
- данные артефактов сохраняются в output/*.json
- текстовая сводка/решения по расхождениям вносятся отдельно в docs/progress.md (вне этого скрипта)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Any, Optional

from pathlib import Path

# Ensure project root on sys.path for 'src' imports
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Локальный клиент 1С OData
try:
    from src.odata_client import OData1CClient
except Exception as e:
    print(f"ERR: cannot import OData1CClient: {e}", file=sys.stderr)
    sys.exit(1)


def load_config(path: str = "config/odata_config.json") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_output_dir() -> None:
    os.makedirs("output", exist_ok=True)


def fetch_top(client: OData1CClient, entity: str, top: int = 5) -> List[Dict[str, Any]]:
    """
    Выполнить GET к EntitySet с параметром $top.
    Возвращает список записей (dict).
    """
    resp = client._make_request(entity, params={"$top": top})
    if isinstance(resp, dict) and isinstance(resp.get("value"), list):
        return resp["value"]
    elif resp:
        # одиночный объект (редкий случай)
        return [resp] if isinstance(resp, dict) else []
    return []


def collect_top_fields(rows: List[Dict[str, Any]]) -> List[str]:
    """
    Собрать множество ключей верхнего уровня по первым N записям.
    """
    fields = set()
    for r in rows:
        if isinstance(r, dict):
            fields.update(r.keys())
    return sorted(fields)


def save_sample(entity: str, rows: List[Dict[str, Any]]) -> str:
    safe_entity = entity.replace("/", "_")
    out_path = f"output/odata_sample_{safe_entity}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"entity": entity, "count": len(rows), "value": rows}, f, ensure_ascii=False, indent=2)
    return out_path


def expected_fields_map() -> Dict[str, List[str]]:
    """
    Ожидаемые поля по документации docs/сущности_1С.md (верхний уровень).
    Важно: многие навигационные/расширенные поля (Номенклатура/Code и т.п.) формируются через $expand/$select и
    могут не присутствовать в плоском ответе. Мы фиксируем базовые ключи.
    """
    return {
        # 1. Catalog_Номенклатура
        "Catalog_Номенклатура": [
            "Ref_Key", "Code", "Description",
            "Артикул", "СпособПополнения", "СрокПополнения",
            "ЕдиницаИзмерения_Key", "КатегорияНоменклатуры_Key", "ТипНоменклатуры",
        ],
        # 1.1. Catalog_КатегорииНоменклатуры
        "Catalog_КатегорииНоменклатуры": [
            "Ref_Key", "Code", "Description", "Parent_Key",
            "IsFolder", "Predefined", "PredefinedDataName",
            "DataVersion", "DeletionMark",
        ],
        # 3. AccumulationRegister_ЗапасыНаСкладах
        "AccumulationRegister_ЗапасыНаСкладах": [
            "Номенклатура_Key", "Склад_Key", "КоличествоОстаток",
            # навигационные могут отсутствовать без $expand:
            # "Номенклатура/Code", "Номенклатура/Description", "Номенклатура/Артикул"
        ],
        # 4. Catalog_Спецификации (карточка спецификации)
        "Catalog_Спецификации": [
            "Ref_Key", "Code", "Description",
            # Состав/Операции чаще отдельными наборами (_Состав, _Операции)
        ],
        # 4.x состав/операции как отдельные наборы
        "Catalog_Спецификации_Состав": [
            "Номенклатура_Key", "Количество", "Этап_Key", "ТипСтрокиСостава",
        ],
        "Catalog_Спецификации_Операции": [
            "Операция_Key", "НормаВремени", "Этап_Key",
        ],
        # 5. Документ ЗаказНаПроизводство (карточка)
        "Document_ЗаказНаПроизводство": [
            "Ref_Key", "Number", "Date", "Posted",
        ],
        "Document_ЗаказНаПроизводство_Продукция": [
            "Номенклатура_Key", "Количество", "Спецификация_Key", "Этап_Key",
        ],
        "Document_ЗаказНаПроизводство_Запасы": [
            "Номенклатура_Key", "Количество", "Спецификация_Key", "Этап_Key",
        ],
        "Document_ЗаказНаПроизводство_Операции": [
            "Операция_Key", "КоличествоПлан", "НормаВремени", "Нормочасы", "Этап_Key",
        ],
        # 6. Документ ЗаказПоставщику (карточка)
        "Document_ЗаказПоставщику": [
            "Ref_Key", "Number", "Date", "Posted", "Контрагент_Key", "СуммаДокумента",
        ],
        "Document_ЗаказПоставщику_Запасы": [
            "Номенклатура_Key", "Количество", "Цена", "Сумма", "ДатаПоступления",
        ],
        # 7. Регистр сведений
        "InformationRegister_СпецификацииПоУмолчанию": [
            "Номенклатура_Key", "Характеристика_Key", "Спецификация_Key",
        ],
    }


def compare_fields(actual_fields: List[str], expected_fields: Optional[List[str]]) -> Dict[str, Any]:
    """
    Сравнить списки полей. Возвращает словарь с missing/extra.
    """
    expected_fields = expected_fields or []
    act = set(actual_fields)
    exp = set(expected_fields)
    return {
        "expected_fields": sorted(expected_fields),
        "missing_fields": sorted(list(exp - act)),
        "extra_fields": sorted(list(act - exp)),
    }


def main(argv: Optional[List[str]] = None) -> int:
    cfg = load_config()
    client = OData1CClient(
        base_url=str(cfg.get("base_url") or "").strip(),
        username=cfg.get("username"),
        password=cfg.get("password"),
        token=cfg.get("token"),
    )

    ents: List[str] = [
        # Полный перечень для проверки
        "Catalog_Номенклатура",
        "Catalog_КатегорииНоменклатуры",
        "AccumulationRegister_ЗапасыНаСкладах",
        "Catalog_Спецификации",
        "Catalog_Спецификации_Состав",
        "Catalog_Спецификации_Операции",
        "Document_ЗаказНаПроизводство",
        "Document_ЗаказНаПроизводство_Продукция",
        "Document_ЗаказНаПроизводство_Запасы",
        "Document_ЗаказНаПроизводство_Операции",
        "Document_ЗаказПоставщику",
        "Document_ЗаказПоставщику_Запасы",
        "InformationRegister_СпецификацииПоУмолчанию",
    ]

    expected_map = expected_fields_map()
    ensure_output_dir()

    summary: List[Dict[str, Any]] = []

    for ent in ents:
        row_count = 0
        fields: List[str] = []
        diff: Dict[str, Any] = {}
        out_file = None
        err_text = None

        try:
            rows = fetch_top(client, ent, top=5)
            row_count = len(rows)
            fields = collect_top_fields(rows)
            out_file = save_sample(ent, rows)
            diff = compare_fields(fields, expected_map.get(ent))
            print(f"OK {ent} -> {out_file} (rows={row_count})")
        except Exception as e:
            err_text = str(e)
            print(f"ERR {ent}: {err_text}", file=sys.stderr)

        item = {
            "entity": ent,
            "count": row_count,
            "fields": fields,
            **({"expected_fields": diff.get("expected_fields", [])} if diff else {}),
            **({"missing_fields": diff.get("missing_fields", [])} if diff else {}),
            **({"extra_fields": diff.get("extra_fields", [])} if diff else {}),
        }
        if out_file:
            item["sample_file"] = out_file
        if err_text:
            item["error"] = err_text

        summary.append(item)

    # Итоговый JSON-отчет
    summary_path = "output/odata_probe_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # Дублируем в stdout (можно копировать в progress.md вручную)
    print("\n=== SUMMARY (also saved to output/odata_probe_summary.json) ===")
    try:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception:
        # На всякий случай — если консоль не UTF-8
        print(json.dumps(summary, ensure_ascii=True, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())