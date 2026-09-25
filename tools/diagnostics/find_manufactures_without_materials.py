"""Read-only OData scan for assemblies with an empty materials table."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "backend"))
sys.path.insert(0, str(Path.cwd()))

from app.services.odata_client import OData1CClient
from app.services.odata_config import load_odata_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("end must be on or after start")
    config = load_odata_config()
    if not config.get("base_url"):
        raise RuntimeError("OData runtime config has no base_url")
    client = OData1CClient(**{k: config.get(k) for k in ("base_url", "username", "password", "token")})
    query = (
        f"Date ge datetime'{args.start}T00:00:00' and "
        f"Date lt datetime'{args.end + timedelta(days=1)}T00:00:00' and DeletionMark eq false"
    )
    documents = []
    seen = set()
    expected = None
    for page in range(10000):
        with contextlib.redirect_stdout(io.StringIO()):
            response = client._make_request("Document_СборкаЗапасов", {
                "$filter": query, "$select": "Ref_Key,Number,Date,Posted,DeletionMark,Автор_Key,ЗаказНаПроизводство_Key,Продукция,Запасы",
                "$orderby": "Ref_Key", "$top": 200, "$skip": len(documents),
                "$inlinecount": "allpages", "$format": "json",
            }, timeout=60, retries=1)
        if not isinstance(response.get("value"), list):
            raise RuntimeError("OData response has no value array")
        count = response.get("odata.count", response.get("@odata.count"))
        if count is not None:
            if expected is not None and expected != int(count):
                raise RuntimeError("Source count changed during scan; rerun")
            expected = int(count)
        rows = response["value"]
        if not rows:
            break
        for row in rows:
            ref = row.get("Ref_Key")
            if not ref or ref in seen:
                raise RuntimeError("Missing or duplicate document key; incomplete scan")
            if not isinstance(row.get("Запасы"), list) or not isinstance(row.get("Продукция"), list):
                raise RuntimeError("OData omitted document tables; cannot classify as empty")
            seen.add(ref)
            documents.append(row)
        print(f"Read {len(documents)} / {expected if expected is not None else '?'} documents", flush=True)
        if expected is not None and len(documents) == expected:
            break
    else:
        raise RuntimeError("Page limit reached")
    if expected is not None and len(documents) != expected:
        raise RuntimeError("Document count mismatch; incomplete scan")
    empty = [row for row in documents if not row["Запасы"]]
    empty.sort(key=lambda row: (row["Date"], row["Number"]))
    summary = {
        "start": str(args.start), "end_inclusive": str(args.end),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "filter": query, "definition": "Запасы is an explicitly returned empty array",
        "documents_scanned": len(documents), "odata_count": expected,
        "posted_scanned": sum(bool(row.get("Posted")) for row in documents),
        "without_materials": len(empty),
        "posted_without_materials": sum(bool(row.get("Posted")) for row in empty),
        "unposted_without_materials": sum(not bool(row.get("Posted")) for row in empty),
    }
    cache = {}

    def reference(entity: str, ref: str) -> dict:
        if not ref or ref == "00000000-0000-0000-0000-000000000000":
            return {}
        key = (entity, ref)
        if key not in cache:
            with contextlib.redirect_stdout(io.StringIO()):
                cache[key] = client._make_request(f"{entity}(guid'{ref}')", {"$format": "json"}, timeout=60, retries=1)
        return cache[key]

    for row in empty:
        author = reference("Catalog_Пользователи", row.get("Автор_Key", ""))
        row["author_name"] = author.get("Description", "Не указан")
        order = reference("Document_ЗаказНаПроизводство", row.get("ЗаказНаПроизводство_Key", ""))
        row["order_number"] = order.get("Number", "")
        row["order_material_rows"] = len(order["Запасы"]) if isinstance(order.get("Запасы"), list) else None
        row["products_display"] = []
        for product in row["Продукция"]:
            item = reference("Catalog_Номенклатура", product.get("Номенклатура_Key", ""))
            row["products_display"].append({"name": item.get("Description", product.get("Номенклатура_Key", "")), "qty": product.get("Количество")})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({**summary, "entries": empty}, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output.with_suffix(".source.json").write_text(json.dumps(documents, ensure_ascii=False, indent=2), encoding="utf-8")
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = [
        "# Выпуски без материалов", "",
        f"Период: {args.start:%d.%m.%Y}–{args.end:%d.%m.%Y} включительно. Проверено через OData: {len(documents)} документов, исключены помеченные на удаление.", "",
        f"Найдено: {len(empty)}. Проведено: {summary['posted_without_materials']}. Критерий: пустая табличная часть «Запасы» документа «СборкаЗапасов». Это проверка состава документа, а не расходных движений регистра.", "",
        f"Время чтения (UTC): {summary['retrieved_at']}. В 1С выполнены только GET-запросы.", "",
        "Автор — реквизит документа «Автор». Он не подтверждает, кто выполнил проведение.", "",
        "| Дата | Выпуск | Автор | Проведён | Продукция × количество | Заказ | Строк материалов в заказе |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in empty:
        products = "; ".join(f"{p['name']} × {p['qty']}" for p in row["products_display"])
        values = [row["Date"][:10], row["Number"], row["author_name"], "Да" if row.get("Posted") else "Нет", products, row["order_number"] or "—", row["order_material_rows"] if row["order_material_rows"] is not None else "нет данных"]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    args.output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
