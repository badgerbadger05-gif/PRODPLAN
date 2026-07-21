#!/usr/bin/env python3
"""Инк0-пробник (ГЕЙТ дизайна складского леджера) — READ-ONLY к 1С OData.

Цель: доказать, что движения `AccumulationRegister_ЗапасыНаСкладах` можно
вытянуть по регистратору (Recorder) наших документов и получить знаковые строки
со складом. Ничего не пишет ни в 1С, ни в локальную БД — только GET и печать.

Проверяет (пункты дизайна §8 Инк0):
  (а) синтаксис фильтра по полиморфному Recorder — перебирает варианты;
  (б) знаковую конвенцию RecordType (Receipt/Expense или ВидДвижения);
  (в) имя поля номера строки (LineNumber/НомерСтроки);
  (г) присутствие склада (СтруктурнаяЕдиница) и полиморфные не-склады;
  (д) единицу Количества (базовая?);
  (е) наличие измерения Характеристика;
  (ж) даёт ли Document_ЗаказНаПроизводство движения по этому регистру;
  (з) грубую стоимость запроса по одному Recorder.

Запуск (из backend/, чтобы импортировался app):
  DATABASE_URL=... PYTHONPATH=/home/ivan/PRODPLAN/repo/backend \
    /home/ivan/PRODPLAN/repo/.venv/bin/python \
    /home/ivan/PRODPLAN/repo/tools/inc0_odata_movements_probe.py

Требует заполненного config/odata_config.json (base_url + username/password|token).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from app.services.odata_client import OData1CClient
from app.services.odata_config import load_odata_config

REGISTER = "AccumulationRegister_ЗапасыНаСкладах"
DOC_TYPES = [
    ("Document_СборкаЗапасов", "сборка (приход ГП + расход компонентов одним регистратором — главный кейс)"),
    ("Document_ПеремещениеЗапасов", "перемещение (расход-отправитель + приход-получатель)"),
    ("Document_ЗаказНаПроизводство", "заказ на производство (ожидаем ПУСТО — движет резервы, не запасы)"),
]


def _client() -> OData1CClient:
    cfg = load_odata_config()
    base = (cfg.get("base_url") or "").strip()
    if not base:
        print("!! config/odata_config.json пуст (base_url не задан). Заполни base_url + креды и повтори.")
        sys.exit(2)
    return OData1CClient(
        base_url=base,
        username=cfg.get("username") or None,
        password=cfg.get("password") or None,
        token=cfg.get("token") or None,
    )


def _recent_docs(cli: OData1CClient, doc_entity: str, n: int = 3):
    """Свежие проведённые документы этого типа — берём их Ref_Key как регистраторы."""
    try:
        rows = cli.get_all(
            doc_entity,
            filter_query="Posted eq true",
            select_fields=["Ref_Key", "Number", "Date"],
            top=n, max_records=n, max_pages=1,
            order_by="Date desc",
        )
        return rows or []
    except Exception as e:  # noqa: BLE001
        print(f"   !! не удалось получить документы {doc_entity}: {e}")
        return []


def _try_recorder_filters(cli: OData1CClient, ref_key: str, doc_entity: str):
    """Перебор вероятных форм фильтра движений регистра по регистратору."""
    variants = [
        ("Recorder_Key eq guid'REF'", f"Recorder_Key eq guid'{ref_key}'"),
        ("Recorder eq cast(guid,type)", f"Recorder eq cast(guid'{ref_key}', '{doc_entity}')"),
        ("Recorder eq guid'REF'", f"Recorder eq guid'{ref_key}'"),
    ]
    for label, flt in variants:
        t0 = time.time()
        try:
            rows = cli.get_all(REGISTER, filter_query=flt, top=50, max_records=50, max_pages=1, order_by=None)
            dt = time.time() - t0
            print(f"   [OK] фильтр «{label}» → {len(rows)} строк за {dt:.2f}s")
            return label, rows
        except Exception as e:  # noqa: BLE001
            print(f"   [--] фильтр «{label}» не сработал: {str(e)[:160]}")
    return None, []


def _describe(rows):
    if not rows:
        print("      (строк нет)")
        return
    sample = rows[0]
    keys = list(sample.keys())
    print(f"      поля строки ({len(keys)}): {keys}")
    # эвристики по интересующим полям
    interesting = {}
    for k in keys:
        lk = k.lower()
        if any(s in lk for s in ("recordtype", "виддвижения", "количеств", "quantity", "linenumber",
                                 "номерстроки", "структурная", "склад", "warehouse", "номенклат",
                                 "характеристик", "организац", "period", "единиц", "unit")):
            interesting[k] = sample.get(k)
    print(f"      ключевые значения примера: {json.dumps(interesting, ensure_ascii=False)[:600]}")


def main():
    cli = _client()
    print(f"== Инк0-пробник движений: {REGISTER} ==\nbase_url = {cli.base_url}\n")
    for doc_entity, note in DOC_TYPES:
        print(f"--- {doc_entity} — {note}")
        docs = _recent_docs(cli, doc_entity)
        if not docs:
            print("   (нет свежих проведённых документов этого типа)\n")
            continue
        for d in docs:
            ref = d.get("Ref_Key")
            print(f"   регистратор {d.get('Number')} @ {d.get('Date')}  ref={ref}")
            label, rows = _try_recorder_filters(cli, ref, doc_entity)
            _describe(rows)
            if rows:
                break  # одного успешного примера на тип достаточно
        print()
    print("Готово. Сведи факты (знак, поле строки, склад, характеристика, ЕИ) в §2/§3 дизайна.")


if __name__ == "__main__":
    main()
