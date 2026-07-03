from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .odata_client import OData1CClient
from .odata_config import load_odata_config as _load_odata_config
from .one_c_export_common import clean_ref1c as _clean_ref1c
from .one_c_piecework_export import DONE_STATE_KEY, ORDER_COMPLETION_SUCCESS, PRODUCTION_ORDER_ENTITY


PRODUCTION_ORDER_SYNC_FROM_1C = "2026-05-01T00:00:00"


@dataclass
class CompletionRepairRow:
    ref_key: str
    number: str
    state_key: Optional[str]
    completion: Optional[str]
    status: str
    reason: Optional[str] = None


def repair_prodplan_order_completion_success(
    *,
    dry_run: bool = True,
    allow_production: bool = False,
    number_prefix: str = "PP",
    date_from: str = PRODUCTION_ORDER_SYNC_FROM_1C,
    max_records: int = 5000,
) -> Dict[str, Any]:
    """
    Mark completed PRODPLAN-created 1C production orders as successfully closed.

    1C has two independent concepts on Document_ЗаказНаПроизводство:
    * СостояниеЗаказа_Key = Завершен
    * ВариантЗавершения = Успешно
    Older PRODPLAN exports set only the state, so 1C list filters by
    "Завершение заказа = Успешно" missed those orders.
    """
    prefix = str(number_prefix or "PP").strip()
    if not prefix:
        raise ValueError("number_prefix is required")
    if max_records <= 0:
        raise ValueError("max_records must be positive")
    if not dry_run and not allow_production:
        raise PermissionError("Pass allow_production=true to patch production 1C orders")

    config = _load_odata_config()
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("OData config is not set. Save 1C connection settings first.")
    client = OData1CClient(
        base_url=base_url,
        username=config.get("username") or None,
        password=config.get("password") or None,
        token=config.get("token") or None,
    )

    filter_query = (
        f"Date ge datetime'{date_from}' and "
        "Posted eq true and "
        f"СостояниеЗаказа_Key eq guid'{DONE_STATE_KEY}'"
    )
    rows = client.get_all(
        PRODUCTION_ORDER_ENTITY,
        filter_query=filter_query,
        select_fields=[
            "Ref_Key",
            "Number",
            "Posted",
            "СостояниеЗаказа_Key",
            "ВариантЗавершения",
        ],
        top=1000,
        max_records=int(max_records),
        max_pages=1000,
        order_by="Number",
    )

    candidates: List[CompletionRepairRow] = []
    patched = 0
    already_ok = 0
    skipped_non_empty = 0
    skipped_by_number = 0
    errors: List[Dict[str, str]] = []

    for rec in rows or []:
        number = str(rec.get("Number") or "").strip()
        ref_key = _clean_ref1c(rec.get("Ref_Key"))
        if not number.startswith(prefix):
            skipped_by_number += 1
            continue
        completion = str(rec.get("ВариантЗавершения") or "").strip() or None
        row = CompletionRepairRow(
            ref_key=ref_key,
            number=number,
            state_key=_clean_ref1c(rec.get("СостояниеЗаказа_Key")) or None,
            completion=completion,
            status="planned",
        )
        if completion == ORDER_COMPLETION_SUCCESS:
            row.status = "already_ok"
            row.reason = "ВариантЗавершения уже Успешно"
            already_ok += 1
            candidates.append(row)
            continue
        if completion:
            row.status = "skipped"
            row.reason = f"ВариантЗавершения уже заполнен: {completion}"
            skipped_non_empty += 1
            candidates.append(row)
            continue
        if not ref_key:
            row.status = "error"
            row.reason = "empty Ref_Key"
            errors.append({"number": number, "error": row.reason})
            candidates.append(row)
            continue
        if dry_run:
            row.status = "dry_run"
            candidates.append(row)
            continue
        try:
            client.patch(
                f"{PRODUCTION_ORDER_ENTITY}(guid'{ref_key}')",
                {"ВариантЗавершения": ORDER_COMPLETION_SUCCESS},
                timeout=60,
            )
            row.status = "patched"
            patched += 1
        except Exception as exc:
            row.status = "error"
            row.reason = str(exc)
            errors.append({"number": number, "ref_key": ref_key, "error": str(exc)})
        candidates.append(row)

    return {
        "status": "ok" if not errors else "partial_error",
        "dry_run": bool(dry_run),
        "entity": PRODUCTION_ORDER_ENTITY,
        "number_prefix": prefix,
        "date_from": date_from,
        "rows_loaded": len(rows or []),
        "skipped_by_number": skipped_by_number,
        "candidates": len(candidates),
        "already_ok": already_ok,
        "skipped_non_empty": skipped_non_empty,
        "patched": patched,
        "errors": errors,
        "rows": [asdict(row) for row in candidates],
    }
