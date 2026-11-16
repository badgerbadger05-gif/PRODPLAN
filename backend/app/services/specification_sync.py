from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, List
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import (
    Specification,
    SpecComponent,
    Operation,
    SpecOperation,
    Item,
    ProductionStage,
    ProductionKind,
    ProductionManufactureOperation,
)
from ..schemas import ODataSyncRequest

_ZERO_GUID = "00000000-0000-0000-0000-000000000000"


def _norm_component_spec_ref(value) -> Optional[str]:
    """
    Нормализует Спецификация_Key строки состава 1С к каноничному виду.

    Пустая строка и нулевой GUID -> None (компонент идёт по основной спецификации).
    Иначе — guid в нижнем регистре. Значимо только для строк типа Сборка/Узел.
    """
    raw = str(value or "").strip().lower()
    if not raw or raw == _ZERO_GUID:
        return None
    return raw


def _sync_spec_components(
    db: Session,
    spec_id: int,
    components_data: list,
    *,
    existing_items: dict[str, Item],
    existing_stages: dict[str, ProductionStage],
) -> tuple[int, int, int]:
    existing = (
        db.query(SpecComponent)
        .filter_by(spec_id=spec_id)
        .order_by(SpecComponent.component_id)
        .all()
    )
    stored_rows = defaultdict(list)
    stored_stages = defaultdict(set)
    for row in existing:
        key = (
            int(row.item_id),
            _norm_component_spec_ref(row.component_spec_ref1c),
            row.component_type or "Материал",
        )
        stored_rows[(*key, row.stage_id)].append(row)
        if row.stage_id is not None:
            stored_stages[key].add(row.stage_id)

    incoming_rows = []
    incoming_stages = defaultdict(set)
    for value in components_data:
        # List and exact OData readers can represent a composition row by
        # LineNumber; Ref_Key is not always included on tabular parts.
        identity = str(value.get("LineNumber") or value.get("Ref_Key") or "").strip()
        if not identity:
            raise ValueError("строка состава не содержит LineNumber или Ref_Key")
        item_ref = str(value.get("Номенклатура_Key") or "").strip()
        item = existing_items.get(item_ref)
        if item is None:
            raise ValueError(f"строка состава {identity}: неизвестная номенклатура {item_ref!r}")
        raw_qty = value.get("Количество")
        if raw_qty is None or raw_qty == "":
            raise ValueError(f"строка состава {identity}: отсутствует количество")
        quantity = Decimal(str(raw_qty))
        if not quantity.is_finite():
            raise ValueError(f"строка состава {identity}: недопустимое количество")
        key = (
            int(item.item_id),
            _norm_component_spec_ref(value.get("Спецификация_Key")),
            value.get("ТипСтрокиСостава") or "Материал",
        )
        stage = existing_stages.get((value.get("Этап_Key") or "").strip())
        stage_id = stage.stage_id if stage else None
        if stage_id is not None:
            incoming_stages[key].add(stage_id)
        incoming_rows.append((key, stage_id, quantity))

    # 1C may repeat the same material in several composition lines. Sum only
    # equivalent rows and keep quantities at each explicit stage and child pin.
    quantities = defaultdict(Decimal)
    for key, stage_id, qty in incoming_rows:
        if stage_id is None:
            # OData may omit a stage that was curated locally. Retain it if
            # there is a unique stored stage not already explicit in the input.
            candidates = stored_stages[key] - incoming_stages[key]
            if len(candidates) > 1:
                raise ValueError(f"неоднозначный этап состава для item_id={key[0]}, spec_id={spec_id}")
            stage_id = next(iter(candidates), None)
        quantities[(*key, stage_id)] += qty

    # Match the precision of SpecComponent.quantity after summing source rows.
    quantities = {key: qty.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP) for key, qty in quantities.items()}
    kept_ids = set()
    created = updated = deleted = 0
    for key, quantity in quantities.items():
        item_id, spec_ref, component_type, stage_id = key
        candidates = stored_rows.get(key)
        if candidates:
            row = candidates[0]
            kept_ids.add(row.component_id)
            if (
                row.quantity != quantity
                or row.component_spec_ref1c != spec_ref
                or row.component_type != component_type
            ):
                row.quantity = quantity
                row.component_spec_ref1c = spec_ref
                row.component_type = component_type
                updated += 1
        else:
            db.add(SpecComponent(
                spec_id=spec_id,
                item_id=item_id,
                quantity=quantity,
                stage_id=stage_id,
                component_type=component_type,
                component_spec_ref1c=spec_ref,
            ))
            created += 1
    for row in existing:
        if row.component_id not in kept_ids:
            db.delete(row)
            deleted += 1
    return created, updated, deleted


@dataclass
class SpecificationSyncStats:
    """Статистика синхронизации спецификаций"""
    specs_total: int = 0
    specs_created: int = 0
    specs_updated: int = 0
    specs_unchanged: int = 0
    components_created: int = 0
    components_updated: int = 0
    components_deleted: int = 0
    components_failed: int = 0
    operations_created: int = 0
    operations_updated: int = 0
    spec_operations_created: int = 0
    spec_operations_updated: int = 0
    spec_operations_deleted: int = 0
    revisions_created: int = 0
    rebase_requests_queued: int = 0
    changed_spec_refs: List[str] | None = None
    dry_run: bool = False
    odata_url: str = ""
    odata_entity: str = ""


def sync_specifications_from_odata(db: Session, req: ODataSyncRequest) -> dict:
    """
    Синхронизация спецификаций из 1С через OData.

    Алгоритм:
    1. Загружаем все записи из Catalog_Спецификации
    2. Для каждой спецификации создаем или обновляем Specification
    3. Обрабатываем вложенные структуры:
       - Состав спецификаций (SpecComponent)
       - Операции (Operation и SpecOperation)
    4. Обновляем статистику синхронизации
    """
    from ..services.odata_client import OData1CClient

    stats = SpecificationSyncStats(
        dry_run=bool(req.dry_run),
        odata_url=req.base_url,
        odata_entity=req.entity_name,
    )

    try:
        # Создаем клиент OData
        client = OData1CClient(req.base_url, req.username, req.password, req.token)

        # Получаем все записи спецификаций.
        # IMPORTANT:
        # - Для корректной синхронизации состава/операций и работы reconcile нам нужны поля 'Состав' и 'Операции'.
        # - Пользовательский $select может их случайно не включить, что приводит к «зависшим» строкам в БД.
        # Поэтому принудительно добавляем эти поля к select_fields.
        base_select = list(req.select_fields or [])
        required_fields = ["Ref_Key", "Code", "Description", "ВидПроизводства_Key", "Состав", "Операции"]
        for f in required_fields:
            if f not in base_select:
                base_select.append(f)
        effective_select = base_select or None

        try:
            spec_data = client.get_all(
                req.entity_name,
                filter_query=req.filter_query,
                select_fields=effective_select,
            )
        except Exception as e:
            # Fallback: на некоторых конфигурациях 1С узкий $select может падать.
            # Повторяем запрос без $select, чтобы всё же получить вложенные табличные части.
            print(f"[spec.sync] primary get_all failed, retry without $select: {e}")
            spec_data = client.get_all(
                req.entity_name,
                filter_query=req.filter_query,
                select_fields=None,
            )

        if not spec_data:
            stats.dry_run = True
            return asdict(stats)

        stats.specs_total = len(spec_data)

        # Получаем существующие записи для сопоставления
        existing_specs = {spec.spec_ref1c: spec for spec in db.query(Specification).all() if spec.spec_ref1c}
        # First deployment has no hashes yet. Snapshot the current local BOM
        # before applying the incoming OData payload, otherwise the first real
        # old->new transition would be mistaken for an initial baseline.
        from .specification_revision import record_specification_revisions

        missing_hash_ids = [
            int(spec.spec_id)
            for spec in existing_specs.values()
            if not spec.content_hash
        ]
        baseline_stats = record_specification_revisions(
            db,
            missing_hash_ids,
            previous_hash_by_id={spec_id: None for spec_id in missing_hash_ids},
            source="local_baseline",
        )
        previous_hash_by_id = {
            int(spec.spec_id): (str(spec.content_hash) if spec.content_hash else None)
            for spec in existing_specs.values()
        }
        touched_spec_ids: set[int] = set()
        existing_operations = {op.operation_ref1c: op for op in db.query(Operation).all() if op.operation_ref1c}
        existing_production_kinds = {pk.ref_1c: pk for pk in db.query(ProductionKind).all() if pk.ref_1c}

        # Получаем существующие номенклатуру и этапы для связей
        existing_items = {item.item_ref1c: item for item in db.query(Item).all() if item.item_ref1c}
        existing_stages = {stage.stage_ref1c: stage for stage in db.query(ProductionStage).all() if stage.stage_ref1c}

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        components_created = 0
        components_updated = 0
        components_deleted = 0
        components_failed = 0
        operations_created = 0
        operations_updated = 0
        spec_operations_created = 0
        spec_operations_updated = 0
        spec_operations_deleted = 0

        # Обрабатываем каждую спецификацию
        for record in spec_data:
            ref_key = ''
            try:
                ref_key = record.get('Ref_Key', '').strip()
                if not ref_key:
                    continue

                # Извлекаем данные спецификации
                code = record.get('Code', '').strip()
                name = record.get('Description', '').strip()

                if not name:
                    continue

                # Обрабатываем состав спецификаций
                # IMPORTANT: reconcile (delete removed rows) выполняем только если поле реально присутствует в ответе OData.
                # Если поле не выгружено (select_fields не содержит вложенную табличную часть), мы НЕ имеем права удалять строки.
                has_components_field = 'Состав' in record
                components_data_raw = record.get('Состав', None)
                components_data: list = []
                can_reconcile_components = False
                if isinstance(components_data_raw, list):
                    components_data = components_data_raw
                    can_reconcile_components = bool(has_components_field)
                else:
                    components_data = []
                    can_reconcile_components = False

                # Обрабатываем операции
                has_operations_field = 'Операции' in record
                operations_data_raw = record.get('Операции', None)
                operations_data: list = []
                can_reconcile_operations = False
                if isinstance(operations_data_raw, list):
                    operations_data = operations_data_raw
                    can_reconcile_operations = bool(has_operations_field)
                else:
                    operations_data = []
                    can_reconcile_operations = False

                # Проверяем, существует ли уже такая спецификация
                existing_spec = existing_specs.get(ref_key)
                current_spec = existing_spec

                # Извлекаем ВидПроизводства из данных
                production_kind_key = record.get('ВидПроизводства_Key', '').strip()
                production_kind = None
                if production_kind_key:
                    production_kind = existing_production_kinds.get(production_kind_key)

                # Header and composition must describe the same source
                # version. A bad source row cannot turn unread materials into
                # obsolete rows, nor create a selectable header-only spec.
                try:
                    with db.begin_nested():
                        production_kind_id = production_kind.id if production_kind else None
                        if existing_spec:
                            needs_update = (
                                existing_spec.spec_code != code
                                or existing_spec.spec_name != name
                                or existing_spec.production_kind_id != production_kind_id
                            )
                            if needs_update:
                                existing_spec.spec_code = code
                                existing_spec.spec_name = name
                                existing_spec.production_kind_id = production_kind_id
                        else:
                            current_spec = Specification(
                                spec_code=code,
                                spec_name=name,
                                spec_ref1c=ref_key,
                                production_kind_id=production_kind_id,
                            )
                            db.add(current_spec)
                            db.flush()

                        created = updated = deleted = 0
                        if has_components_field and not can_reconcile_components:
                            raise ValueError("табличная часть состава не является списком")
                        if can_reconcile_components:
                            created, updated, deleted = _sync_spec_components(
                                db,
                                int(current_spec.spec_id),
                                components_data,
                                existing_items=existing_items,
                                existing_stages=existing_stages,
                            )
                        db.flush()
                except Exception as error:
                    components_failed += 1
                    print(f"Ошибка синхронизации состава спецификации {ref_key}: {error}")
                    continue

                if not existing_spec:
                    created_count += 1
                    existing_specs[ref_key] = current_spec
                elif needs_update:
                    updated_count += 1
                else:
                    unchanged_count += 1
                components_created += created
                components_updated += updated
                components_deleted += deleted
                touched_spec_ids.add(int(current_spec.spec_id))

                # Обрабатываем операции спецификации
                seen_operation_ids: set[int] = set()
                for op_data in operations_data:
                    try:
                        operation_key = op_data.get('Операция_Key', '').strip()
                        time_norm = op_data.get('НормаВремени', 0.0)
                        stage_key = op_data.get('Этап_Key', '').strip()

                        if not operation_key:
                            continue

                        # Находим или создаем операцию
                        operation = existing_operations.get(operation_key)
                        if not operation:
                            operation = Operation(
                                operation_ref1c=operation_key,
                                time_norm=time_norm
                            )
                            db.add(operation)
                            # Получить operation_id, чтобы корректно создавать SpecOperation
                            db.flush()
                            operations_created += 1
                            # Обновить локальный индекс, чтобы избежать повторных вставок с тем же ключом
                            existing_operations[operation_key] = operation
                        else:
                            if operation.time_norm != time_norm:
                                operation.time_norm = time_norm
                                operations_updated += 1
                            # Страхуемся, что в индексе присутствует объект операции
                            existing_operations[operation_key] = operation

                        if operation and operation.operation_id is not None:
                            seen_operation_ids.add(int(operation.operation_id))

                        # Находим этап
                        stage = existing_stages.get(stage_key) if stage_key else None

                        # Создаем или обновляем связь спецификация-операция
                        existing_spec_op = db.query(SpecOperation).filter_by(
                            spec_id=current_spec.spec_id,
                            operation_id=operation.operation_id
                        ).first()

                        if existing_spec_op:
                            # Same preservation rule as for components: an
                            # absent stage in OData must not wipe local stage
                            # knowledge used by capacity/MRP views.
                            desired_stage_id = stage.stage_id if stage else existing_spec_op.stage_id
                            updated = False
                            if existing_spec_op.stage_id != (desired_stage_id):
                                existing_spec_op.stage_id = desired_stage_id
                                updated = True
                            if existing_spec_op.time_norm != time_norm:
                                existing_spec_op.time_norm = time_norm
                                updated = True
                            if updated:
                                spec_operations_updated += 1
                        else:
                            new_spec_op = SpecOperation(
                                spec_id=current_spec.spec_id,
                                operation_id=operation.operation_id,
                                stage_id=stage.stage_id if stage else None,
                                time_norm=time_norm
                            )
                            db.add(new_spec_op)
                            spec_operations_created += 1

                    except Exception as e:
                        print(f"Ошибка обработки операции спецификации {ref_key}: {e}")
                        continue

                # Reconcile: удалить связи спецификация-операция, которые больше не присутствуют в 1С (только если поле 'Операции' выгружено).
                # Строки, на которые ссылаются операции изготовлений
                # (production_manufacture_operations.spec_operation_id), не удаляем:
                # изготовление обязано сохранять свою производственную основу,
                # а попытка удаления валит FK и абортирует транзакцию.
                if can_reconcile_operations:
                    try:
                        with db.begin_nested():
                            referenced_ops = (
                                db.query(ProductionManufactureOperation.spec_operation_id)
                                .filter(ProductionManufactureOperation.spec_operation_id.isnot(None))
                                .subquery()
                            )
                            q_del = db.query(SpecOperation).filter(
                                SpecOperation.spec_id == current_spec.spec_id,
                                ~SpecOperation.spec_operation_id.in_(referenced_ops.select()),
                            )
                            if seen_operation_ids:
                                q_del = q_del.filter(~SpecOperation.operation_id.in_(list(seen_operation_ids)))
                            deleted = q_del.delete(synchronize_session=False)
                            if deleted:
                                spec_operations_deleted += int(deleted)
                    except Exception as e:
                        print(f"Ошибка reconcile операций спецификации {ref_key}: {e}")

            except Exception as e:
                # Логируем ошибку, но продолжаем обработку
                print(f"Ошибка обработки спецификации {ref_key}: {e}")
                if not db.is_active:
                    # После ошибки flush сессия непригодна: все накопленные
                    # изменения уже потеряны, каждая следующая спецификация
                    # упадёт каскадом PendingRollbackError. Поднимаем исходную
                    # ошибку, чтобы job завершился с настоящей причиной.
                    raise
                continue

        # Сохраняем изменения
        stats.specs_created = created_count
        stats.specs_updated = updated_count
        stats.specs_unchanged = unchanged_count
        stats.components_created = components_created
        stats.components_updated = components_updated
        stats.components_deleted = components_deleted
        stats.components_failed = components_failed
        stats.operations_created = operations_created
        stats.operations_updated = operations_updated
        stats.spec_operations_created = spec_operations_created
        stats.spec_operations_updated = spec_operations_updated
        stats.spec_operations_deleted = spec_operations_deleted

        # Child reconciliation may use bulk DELETE, therefore flush before
        # hashing the planning-significant content.  Sync automatically emits a
        # durable request but never starts one heavy MRP per OData row.
        db.flush()
        revision_stats = record_specification_revisions(
            db,
            touched_spec_ids,
            previous_hash_by_id=previous_hash_by_id,
            source="odata",
        )
        stats.revisions_created = int(
            baseline_stats["revisions_created"]
        ) + int(revision_stats["revisions_created"])
        stats.rebase_requests_queued = int(
            revision_stats["rebase_requests_queued"]
        )
        stats.changed_spec_refs = list(revision_stats["changed_spec_refs"])

        if req.dry_run:
            db.rollback()
        else:
            db.commit()
            # Состав BOM определяет пары «окраска → предшественник». После
            # штатной синхронизации составов справочник обновляется сам.
            from .paint_weld_pairs import rebuild_auto_pairs

            result = asdict(stats)
            try:
                result["paint_weld_pairs"] = rebuild_auto_pairs(db)
            except Exception as pair_error:
                db.rollback()
                result["paint_weld_pairs"] = {
                    "status": "error",
                    "error": str(pair_error),
                }
            return result

    except Exception as e:
        db.rollback()
        raise Exception(f"Ошибка синхронизации спецификаций: {e}")

    return asdict(stats)
