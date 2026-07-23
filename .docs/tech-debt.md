# Технический долг / известные мины

Живой список. Закрыл пункт — удали строку. История и контекст: `archive/review_fixes_status.md`, `archive/code_review_merged.md`.

## Инфраструктура (из код-ревью, осталось)

- **B1 — несущий `create_all`.** `Base.metadata.create_all` на импорте `main.py` — единственное, что строит базовые таблицы: на чистой БД `alembic upgrade head` падает (`relation "items" does not exist`), alembic внедрён поверх готовой схемы (прод — `stamp head`). Лечение: baseline-миграция всей схемы (`down_revision=None`), перецепить `20250925_01`, сверить дрейф с продом, `alembic upgrade head` в entrypoint, убрать `create_all`. Протокол — `archive/review_fixes_status.md` §B1.
- **A1-non-root.** Контейнеры бегут под root: `USER appuser` откатили (не мог писать в смонтированные `./output`/`./config`). Правильно: `user: "<uid barsukov>:<gid>"` в `docker-compose.yml` для backend/sync-worker/reconcile-worker, без хардкода uid в Dockerfile.
- Остаточные M-4 (низкий приоритет): pre-generated `Ref_Key` до POST, `ON CONFLICT` в `upsert_sync_link`, ретраи PATCH/POST в `odata_client`.

## Item-ledger (блокеры включения `bin` закрыты волнами 1–3 от 23.07; остаток — не блокеры)

Код: `backend/app/services/item_ledger/`. Обзор: [`item_ledger.md`](item_ledger.md). Закрыто: матчинг (цепочка sync_link → order_ref → FIFO → unplanned), сид T0, дренаж очереди, attempts/in-flight, unrealize при перепроведении, release/cancel/reopen, пины-сироты, сверка (агрегат по характеристикам + discovery документа-источника), триггер т1, семантика contour-exit для transfer, возвраты.

1. **Мониторинг `unplanned_consumption`** (не баг): документы без связи (шапка без основания и не наш экспорт) честно падают в unplanned — следить за счётчиком в summary `realize_from_sle` после включения на стенде.
2. **Свободный приход не участвует в Pass C.** Незапиненные строки прихода без резерва-получателя не попадают в redistribute-пул → surplus не раздаётся (ORM-адаптер сужает вход; чистая `redistribute()` полный Pass C умеет).
3. **PeggingLink-цепочка компонентов узлов** (§6.3 шаг 2 дизайна) не строится — компонент производимого узла матчится run-scoped FIFO, не точным пеггингом.
4. **MrpFreezeAllocation → SQL-VIEW** (инк6в) не сделан — живёт dual-write в reservation_coverage.
5. **Мёртвые carry-артефакты схемы.** Таблица `mrp_requirement_carry`, поля `prior_run_id`, `carried_remaining` — от отброшенного carry-дизайна, код их не использует. Снести миграцией.

## DBR

- **Три читателя `ItemWarehouseStock` мимо флага `STOCK_SOURCE`:** `services/dbr/adapters.py:271`, `services/dbr/feeder_nfp_service.py:64` и `:271`, `services/dbr/purchase_materialize_service.py:519`. При переключении на `bin` DBR продолжит читать legacy-остатки.
- **Мёртвый код:** `services/dbr/core/feeder/group_load.py` (`build_group_load`) — портирован из prodflow, но не вызывается. Кандидат на оживление: проверка исполнимости ΔB по мощности групп мехцеха в месячном расчёте буферов (decisions-log §7.3).
- **DBR×MRP — только guard, не allocation:** `_dbr_owned_qty` защищает позицию от MRP-ресайза целиком; частичное покрытие двух контуров требует явной таблицы allocation сигнал↔требование (см. `unified_production_journal.md`).

## Целевая модель (крупные стройки впереди)

- Флаг «буферизована/MRP» на номенклатуре + месячный расчёт прогнозных буферов B_p(t) и эшелонных запусков (decisions-log §7.2–7.3, разрез ABC/XYZ от 23.07).
- Верхний стык идентичности спроса: lineage DBR-программы (план/ран/ручная).
