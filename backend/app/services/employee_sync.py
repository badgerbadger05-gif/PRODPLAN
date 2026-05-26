from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict

from sqlalchemy.orm import Session

from ..models import Employee
from ..schemas import ODataSyncRequest
from ..services.odata_client import OData1CClient


def _s(val: Any) -> str:
    return str(val or "").strip()


def _bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in {"true", "1", "yes", "да"}


@dataclass
class EmployeeSyncStats:
    """Статистика синхронизации сотрудников из 1С."""

    employees_total: int = 0
    employees_created: int = 0
    employees_updated: int = 0
    employees_unchanged: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def sync_employees_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация справочника сотрудников из 1С через OData.

    Ожидаемая сущность: Catalog_Сотрудники. Основная цель синхронизации -
    сохранить Ref_Key сотрудников для последующего создания сдельных нарядов.
    """
    entity_name = _s(req.entity_name or "Catalog_Сотрудники")
    client = OData1CClient(req.base_url, req.username, req.password, req.token)

    stats = EmployeeSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=entity_name,
    )

    try:
        from ..services.progress_manager import progress  # type: ignore
    except Exception:
        progress = None  # type: ignore

    total_count = 0
    try:
        total_count = client.get_count(entity_name, None)
    except Exception:
        total_count = 0

    if total_count > 0:
        stats.employees_total = int(total_count)

    if progress:
        progress.start("employees", total=total_count or 0, message="Загрузка сотрудников из 1С")

    existing_by_ref: Dict[str, Employee] = {
        e.employee_ref1c: e for e in db.query(Employee).all() if e.employee_ref1c
    }

    created = 0
    updated = 0
    unchanged = 0
    processed = 0

    try:
        for page in client.iter_pages(
            entity_name,
            filter_query=req.filter_query,
            select_fields=req.select_fields,
            top=1000,
            max_pages=1000,
            order_by="Ref_Key",
        ):
            for row in page:
                ref_key = _s(row.get("Ref_Key"))
                if not ref_key:
                    continue

                processed += 1
                if progress and (processed % 50 == 0):
                    msg = f"Обработано {processed}" + (f"/{total_count}" if total_count else "")
                    progress.update("employees", processed=processed, message=msg)

                code = _s(row.get("Code")) or None
                name = (
                    _s(row.get("Description"))
                    or _s(row.get("Наименование"))
                    or _s(row.get("Name"))
                    or code
                    or ref_key
                )
                deletion_mark = _bool(row.get("DeletionMark"))
                data_version = _s(row.get("DataVersion")) or None

                existing = existing_by_ref.get(ref_key)
                if existing:
                    need_update = False
                    if existing.employee_code != code:
                        existing.employee_code = code
                        need_update = True
                    if existing.employee_name != name:
                        existing.employee_name = name
                        need_update = True
                    if bool(existing.deletion_mark) != deletion_mark:
                        existing.deletion_mark = deletion_mark
                        need_update = True
                    if (existing.data_version or None) != data_version:
                        existing.data_version = data_version
                        need_update = True

                    if need_update:
                        updated += 1
                    else:
                        unchanged += 1
                else:
                    employee = Employee(
                        employee_ref1c=ref_key,
                        employee_code=code,
                        employee_name=name,
                        deletion_mark=deletion_mark,
                        data_version=data_version,
                    )
                    db.add(employee)
                    existing_by_ref[ref_key] = employee
                    created += 1

                if (processed % 1000) == 0:
                    db.flush()

        if stats.employees_total == 0:
            stats.employees_total = int(processed)

        stats.employees_created = created
        stats.employees_updated = updated
        stats.employees_unchanged = unchanged

        if progress:
            progress.update(
                "employees",
                processed=processed,
                message=f"Готово: {processed}/{stats.employees_total or processed}",
            )
            progress.finish("employees", error=None, message="Синхронизация сотрудников завершена")

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

        return asdict(stats)

    except Exception as e:
        db.rollback()
        if progress:
            try:
                progress.finish("employees", error=str(e), message="Синхронизация сотрудников завершилась ошибкой")
            except Exception:
                pass
        raise Exception(f"Ошибка синхронизации сотрудников: {e}")
