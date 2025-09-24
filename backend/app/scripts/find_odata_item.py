from __future__ import annotations

import json
import sys
from app.services.odata_client import OData1CClient


def norm(s: object) -> str:
    return str(s or "").strip().replace("-", "").replace(" ", "").replace("\u0413", "G")


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "G0003216"

    with open("config/odata_config.json", encoding="utf-8") as f:
        cfg = json.load(f)

    client = OData1CClient(
        cfg.get("base_url", ""),
        cfg.get("username"),
        cfg.get("password"),
        cfg.get("token"),
    )

    checked = 0
    found = []
    try:
        total = client.get_count("Catalog_Номенклатура")
    except Exception:
        total = None

    try:
        for page in client.iter_pages(
            "Catalog_Номенклатура",
            select_fields=["Ref_Key", "Code", "Description", "Артикул"],
            top=1000,
            max_pages=5000,
        ):
            for r in page:
                checked += 1
                code_raw = r.get("Code")
                art_raw = r.get("Артикул")
                name_raw = r.get("Description")
                code = norm(code_raw)
                art = norm(art_raw)
                name = norm(name_raw)

                if code == target or art == target or (target in name):
                    found.append(
                        {
                            "Ref_Key": r.get("Ref_Key"),
                            "Code": code_raw,
                            "Артикул": art_raw,
                            "Description": name_raw,
                        }
                    )
                    if len(found) >= 5:
                        break
            if found:
                break
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return

    print(
        json.dumps(
            {
                "target": target,
                "total_in_1c": total,
                "checked": checked,
                "found": found,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()