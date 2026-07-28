from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session

from ..models import ProductionKind
from ..schemas import ODataSyncRequest


@dataclass
class ProductionKindSyncStats:
    """Статистика синхронизации видов производства"""
    kinds_total: int = 0
    kinds_created: int = 0
    kinds_updated: int = 0
    kinds_unchanged: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def sync_production_kinds_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация видов производства из 1С через OData.

    Предполагаемая сущность 1С: Catalog_ВидыПроизводства
    Поля по умолчанию:
      - Ref_Key (обяз.)
      - Description (имя вида производства)
      - Наименование (альтернативное имя вида производства)

    Если требуются другие поля или иное имя сущности — передать их через payload.entity_name и select_fields.
    """
    from ..services.odata_client import OData1CClient

    stats = ProductionKindSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    try:
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # Готовим набор полей: если пользователь явно не указал select_fields,
        # используем минимальный безопасный набор (в UNF часто нет поля "Наименование")
        select_fields = req.select_fields or ["Ref_Key", "Description"]
        
        # Загружаем все записи каталога видов производства с устойчивым фолбэком:
        #  - при ошибке "path segment is not found" или "Bad request" повторяем запрос
        #    сначала с минимальным набором полей, затем без $select вовсе.
        try:
            data: List[Dict[str, Any]] = client.get_all(
                req.entity_name,
                filter_query=req.filter_query,
                select_fields=select_fields,
            )
        except Exception as e:
            emsg = str(e).lower()
            if ("path segment is not found" in emsg) or ("bad request" in emsg):
                try:
                    data = client.get_all(
                        req.entity_name,
                        filter_query=req.filter_query,
                        select_fields=["Ref_Key"],
                    )
                except Exception:
                    # Последняя попытка — без $select
                    data = client.get_all(
                        req.entity_name,
                        filter_query=req.filter_query,
                        select_fields=None,
                    )
            else:
                raise

        if not data:
            stats.kinds_total = 0
            # Если пусто — считаем как dry-run, чтобы не коммитить транзакцию зря
            stats.dry_run = True
            return asdict(stats)

        stats.kinds_total = len(data)

        # Индексы существующих записей
        all_kinds = db.query(ProductionKind).all()
        existing_by_ref: Dict[str, ProductionKind] = {
            k.ref_1c: k for k in all_kinds if k.ref_1c
        }
        existing_by_name: Dict[str, ProductionKind] = {
            (k.name or "").strip(): k for k in all_kinds if (k.name or "").strip()
        }

        created = 0
        updated = 0
        unchanged = 0

        for row in data:
            try:
                ref = (row.get("Ref_Key") or "").strip()
                if not ref:
                    continue

                # Имя вида производства может называться по-разному; используем несколько синонимов
                name = (row.get("Description") or row.get("Наименование") or row.get("Представление") or "").strip()
                if not name:
                    # как крайний случай — пробуем более распространённые варианты
                    name = (row.get("Name") or row.get("Наим") or row.get("Desc") or "").strip()
                # На фолбэк-пути ($select=Ref_Key / без $select) 1С не отдаёт
                # наименование. GUID годится как имя только для НОВОЙ записи и
                # никогда — как замена уже известному локальному имени.
                fallback_name = name or ref

                ex = existing_by_ref.get(ref)
                if not ex and name:
                    # Хэндлим случай, когда вид производства уже заведён локально без ref_1c (например, вручную)
                    ex = existing_by_name.get(name)

                if ex:
                    need_update = False
                    # Обновим имя (на всякий случай) и зафиксируем ref_1c
                    new_name = name or (ex.name or "").strip() or fallback_name
                    if (ex.name or "") != new_name:
                        ex.name = new_name
                        need_update = True
                    if (not ex.ref_1c) or (ex.ref_1c != ref):
                        ex.ref_1c = ref
                        need_update = True

                    if need_update:
                        updated += 1
                    else:
                        unchanged += 1

                    # Поддержим индексы в актуальном состоянии
                    existing_by_ref[ref] = ex
                    if name:
                        existing_by_name[name] = ex
                else:
                    # Вставка новой записи
                    new_kind = ProductionKind(
                        name=fallback_name,
                        ref_1c=ref,
                    )
                    db.add(new_kind)
                    created += 1
                    existing_by_ref[ref] = new_kind
                    existing_by_name[fallback_name] = new_kind
            except Exception:
                # Не валим всю синхронизацию из‑за единичной записи
                continue

        stats.kinds_created = created
        stats.kinds_updated = updated
        stats.kinds_unchanged = unchanged

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

        return asdict(stats)

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации видов производства: {e}")