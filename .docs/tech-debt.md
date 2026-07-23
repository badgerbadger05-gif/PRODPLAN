# Технический долг / известные мины

Живой список. Закрыл пункт — удали строку. История и контекст: `archive/review_fixes_status.md`, `archive/code_review_merged.md`.

## Инфраструктура (из код-ревью, осталось)

- **B1 — несущий `create_all`.** `Base.metadata.create_all` на импорте `main.py` — единственное, что строит базовые таблицы: на чистой БД `alembic upgrade head` падает (`relation "items" does not exist`), alembic внедрён поверх готовой схемы (прод — `stamp head`). Лечение: baseline-миграция всей схемы (`down_revision=None`), перецепить `20250925_01`, сверить дрейф с продом, `alembic upgrade head` в entrypoint, убрать `create_all`. Протокол — `archive/review_fixes_status.md` §B1.
- **A1-non-root.** Контейнеры бегут под root: `USER appuser` откатили (не мог писать в смонтированные `./output`/`./config`). Правильно: `user: "<uid barsukov>:<gid>"` в `docker-compose.yml` для backend/sync-worker/reconcile-worker, без хардкода uid в Dockerfile.
- Остаточные M-4 (низкий приоритет): pre-generated `Ref_Key` до POST, `ON CONFLICT` в `upsert_sync_link`, ретраи PATCH/POST в `odata_client`.

## Item-ledger (блокеры включения `STOCK_SOURCE=bin` в прод)

Код: `backend/app/services/item_ledger/`. Обзор: [`item_ledger.md`](item_ledger.md).

1. **Матчинг SLE→резерв: цепочка резолюции реализована** (`_MatchIndex._resolve_order`, ветка ledger-ops/matching-2). Регистратор (GUID СборкаЗапасов/ПеремещениеЗапасов) резолвится в заказ по цепочке: (а) `sync_link` (`manufacture`/`material_issue`, наши экспортированные документы) → локальный `ProductionManufacture`/`ProductionMaterialIssue` → заказ + точная строка; (б) `stock_recorder_pull.order_ref` (GUID заказа из шапки документа, миграция 20260723_04 — документы, созданные в 1С напрямую); (в) вырожденное прямое совпадение (регистратор = GUID заказа). Дальше — прежние три ступени решения №11: pegged → run-scoped FIFO → честный `unplanned_consumption` (без глобального FIFO). Перемещение ГП на склад `is_finished_goods` с основанием-заказом закрывает make-резерв; supplier-приёмки остаются на coverage-стороне (`verify_frozen_supply`), `movement_kind='receipt'` в матчинг сознательно не входит. **Остаток (честный, не баг):** документы без связи (шапка без основания и не наш экспорт) падают в `unplanned_consumption` — мониторить счётчик в summary `realize_from_sle`; двойной issue-путь transfer_out (выдача в цех) + assembly_out (потребление сборкой) реализует consume дважды с капом по outstanding — семантика issue-kinds прежняя.
2. **Нет unrealize-компенсации при replace-by-recorder.** Повторный pull того же регистратора удаляет и заново вставляет его SLE-строки → matching реализует резерв второй раз (двойной realize). Нужен откат `realized_qty` по `reservation_event.sle_id` удалённых строк.
3. **Нет release/cancel резервов при закрытии прогона.** Резервы открытого прогона живут вечно; закрытие/отмена planning_run их не освобождает.
4. **Очередь `stock_recorder_pull` не дренится.** `process_pending_pulls` (ingest.py) не вызывается из прод-кода — только из тестов; записи `pending`/`error` копятся. Плюс косметика: `attempts` инкрементится и на успешном pull (`bump_attempt=True` в success-path).
5. **Свободный приход не участвует в Pass C.** Незапиненные строки прихода без резерва-получателя не попадают в redistribute-пул → surplus не раздаётся.
6. **Мёртвые carry-артефакты схемы.** Таблица `mrp_requirement_carry`, поля `prior_run_id`, `carried_remaining` — от отброшенного carry-дизайна, код их не использует. Снести миграцией.

## DBR

- **Три читателя `ItemWarehouseStock` мимо флага `STOCK_SOURCE`:** `services/dbr/adapters.py:271`, `services/dbr/feeder_nfp_service.py:64` и `:271`, `services/dbr/purchase_materialize_service.py:519`. При переключении на `bin` DBR продолжит читать legacy-остатки.
- **Мёртвый код:** `services/dbr/core/feeder/group_load.py` (`build_group_load`) — портирован из prodflow, но не вызывается.
- **Автопересчёт DBR-очереди в `sync_orchestrator` не реализован** (роадмап `dbr_parallel_module_roadmap.md` §3.4) — после синка барабан/питатели руками.
