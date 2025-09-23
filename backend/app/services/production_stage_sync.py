from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session

from ..models import ProductionStage
from ..schemas import ODataSyncRequest


@dataclass
class ProductionStageSyncStats:
    """Статистика синхронизации этапов производства"""
    stages_total: int = 0
    stages_created: int = 0
    stages_updated: int = 0
    stages_unchanged: int = 0
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def sync_production_stages_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация этапов производства из 1С через OData.

    Предполагаемая сущность 1С: каталог с этапами (например, "Catalog_ЭтапыПроизводства").
    Поля по умолчанию:
      - Ref_Key (обяз.)
      - Description (имя этапа)
      - Порядок / ПорядокСортировки / Order (необяз., порядок сортировки)

    Если требуются другие поля или иное имя сущности — передать их через payload.entity_name и select_fields.
    """
    from ..services.odata_client import OData1CClient

    stats = ProductionStageSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    try:
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # Готовим набор полей: если пользователь явно не указал select_fields, используем минимальный безопасный набор
        # Некоторые ИБ не содержат поля "Порядок"/"ПорядокСортировки"/"Order" и падают на $select
        select_fields = req.select_fields or [
            "Ref_Key",
            "Description"
        ]

        # Загружаем все записи каталога этапов
        data: List[Dict[str, Any]] = client.get_all(
            req.entity_name,
            filter_query=req.filter_query,
            select_fields=select_fields,
        )

        if not data:
            stats.stages_total = 0
            # Если пусто — считаем как dry-run, чтобы не коммитить транзакцию зря
            stats.dry_run = True
            return asdict(stats)

        stats.stages_total = len(data)

        # Индексы существующих записей
        all_stages = db.query(ProductionStage).all()
        existing_by_ref: Dict[str, ProductionStage] = {
            s.stage_ref1c: s for s in all_stages if s.stage_ref1c
        }
        existing_by_name: Dict[str, ProductionStage] = {
            (s.stage_name or "").strip(): s for s in all_stages if (s.stage_name or "").strip()
        }

        created = 0
        updated = 0
        unchanged = 0

        for row in data:
            try:
                ref = (row.get("Ref_Key") or "").strip()
                if not ref:
                    continue

                # Имя этапа может называться по-разному; используем несколько синонимов
                name = (row.get("Description") or row.get("Наименование") or row.get("Представление") or "").strip()
                order = (
                    _to_int(row.get("Порядок"))
                    or _to_int(row.get("ПорядокСортировки"))
                    or _to_int(row.get("Order"))
                )

                ex = existing_by_ref.get(ref)
                if not ex and name:
                    # Хэндлим случай, когда этап уже заведён локально без stage_ref1c (например, вручную)
                    ex = existing_by_name.get(name)

                if ex:
                    need_update = False
                    # Обновим имя (на всякий случай) и зафиксируем ref1c
                    if name and (ex.stage_name or "") != name:
                        ex.stage_name = name
                        need_update = True
                    if (not ex.stage_ref1c) or (ex.stage_ref1c != ref):
                        ex.stage_ref1c = ref
                        need_update = True
                    # Обновим порядок только если явно задан
                    if order is not None and ex.stage_order != order:
                        ex.stage_order = order
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
                    new_stage = ProductionStage(
                        stage_name=name or ref,
                        stage_order=order,
                        stage_ref1c=ref,
                    )
                    db.add(new_stage)
                    created += 1
                    existing_by_ref[ref] = new_stage
                    if name:
                        existing_by_name[name] = new_stage
            except Exception:
                # Не валим всю синхронизацию из‑за единичной записи
                continue

        stats.stages_created = created
        stats.stages_updated = updated
        stats.stages_unchanged = unchanged

        if req.dry_run:
            db.rollback()
        else:
            db.commit()

        return asdict(stats)

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации этапов производства: {e}")