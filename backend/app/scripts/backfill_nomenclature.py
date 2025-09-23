from __future__ import annotations

import json
from typing import Iterable, Dict, Any, Set, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Item
from app.services.odata_client import OData1CClient


def iter_odata_items(client: OData1CClient) -> Iterable[Dict[str, Any]]:
    """
    Надёжный потоковый обход Catalog_Номенклатура (только элементы, без папок).
    Возвращает записи со стандартными полями 1С: Ref_Key, Code, Description, Артикул, ...
    """
    # Жёстко исключаем папки
    base_filter = "IsFolder eq false"
    for page in client.iter_pages(
        "Catalog_Номенклатура",
        filter_query=base_filter,
        select_fields=["Ref_Key", "Code", "Description", "Артикул", "ЕдиницаИзмерения_Key", "КатегорияНоменклатуры_Key", "СпособПополнения", "СрокПополнения"],
        top=1000,
        max_pages=10000,
        order_by="Ref_Key",
    ):
        for rec in page:
            yield rec


def load_existing_codes(db: Session) -> Set[str]:
    rows = db.execute(text("SELECT item_code FROM items")).fetchall()
    return {str(r[0]) for r in rows if r and r[0] is not None}


def upsert_missing_items(db: Session, records: Iterable[Dict[str, Any]], existing_codes: Set[str]) -> int:
    created = 0
    batch_count = 0

    for rec in records:
        code = str((rec.get("Code") or "")).strip()
        if not code:
            continue  # пропускаем некорректные записи без кода
        if code in existing_codes:
            continue  # уже есть

        name = str((rec.get("Description") or "")).strip() or code
        article = str((rec.get("Артикул") or "")).strip() or None
        ref_key = str((rec.get("Ref_Key") or "")).strip() or None
        replenishment_method = str((rec.get("СпособПополнения") or "")).strip() or None
        replenishment_time = rec.get("СрокПополнения")
        unit_key = str((rec.get("ЕдиницаИзмерения_Key") or "")).strip() or None

        item = Item(
            item_code=code,
            item_name=name,
            item_article=article,
            item_ref1c=ref_key,
            replenishment_method=replenishment_method,
            replenishment_time=replenishment_time,
            unit=unit_key,
            stock_qty=0.0,
            status="active",
        )
        db.add(item)
        created += 1
        batch_count += 1
        existing_codes.add(code)

        # периодически сбрасываем изменения чтобы не раздувать транзакцию
        if batch_count >= 500:
            db.flush()
            db.commit()
            batch_count = 0

    # финальный коммит хвоста
    db.flush()
    db.commit()
    return created


def main() -> None:
    # Загружаем доступ к 1С
    with open("config/odata_config.json", encoding="utf-8") as f:
        cfg = json.load(f)

    client = OData1CClient(
        cfg.get("base_url", ""),
        cfg.get("username"),
        cfg.get("password"),
        cfg.get("token"),
    )

    # Подсчёт в 1С
    try:
        total_in_1c = client.get_count("Catalog_Номенклатура", "IsFolder eq false")
    except Exception:
        total_in_1c = None

    db = SessionLocal()
    try:
        before = db.execute(text("SELECT COUNT(*) FROM items")).scalar() or 0
        existing = load_existing_codes(db)

        created = upsert_missing_items(db, iter_odata_items(client), existing)
        after = db.execute(text("SELECT COUNT(*) FROM items")).scalar() or 0

        print(
            json.dumps(
                {
                    "total_in_1c": total_in_1c,
                    "db_before": before,
                    "db_created": created,
                    "db_after": after,
                },
                ensure_ascii=False,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()