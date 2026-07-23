# Архитектура

## Компоненты

- Frontend — React ERP-shell (SPA, порт 9000): `frontend-erp-shell/src/ui/pages/` (страницы), `src/services/` (типизированные API-сервисы), `src/lib/api.ts` (транспорт).
- Backend — FastAPI (порт 8000): `backend/app/main.py` (bootstrap), `routers/` (HTTP), `services/` (логика), `models.py` (SQLAlchemy), `alembic/` (миграции).
- PostgreSQL — все данные приложения и планирования.
- 1С (УНФ) — интеграция через OData, только чтение + выгрузка документов.

## MRP (ядро планирования)

`services/planning_service.py` + `planning_service/`: загрузка конфигурации и исходных данных → брутто/нетто-потребности (`mrp_requirement`) → предложения производства/закупок (`planned_*`) → мощность (`capacity_load`) → pegging и приоритеты. Прогон — `planning_run`, режим дневной. Поверх — freeze/execution-леджер (`mrp_freeze.py`, `mrp_execution_ledger.py`, `mrp_reconciliation.py`): кросс-плановый учёт остатка «stock-once», закрытие планов исполнением, drift-события.

## DBR («Барабан + Питатели», `services/dbr/`)

Параллельный контур планирования: барабан (`drum_service.py`, расписание/слоты/дефициты мощности), питатели супермаркета (`feeder_*_service.py`: позиции, сигналы, NFP, цепочки, материалы), классификация и настройки (`classify.py`, `settings_service.py`), материализация сигналов в заказы производства/закупки (`materialize_service.py`, `purchase_materialize_service.py`), доски (`board_service.py`, `processing_board_service.py`). Чистое ядро, портированное из prodflow, — `dbr/core/`. Роадмап: `dbr_parallel_module_roadmap.md`.

## Item-ledger (`services/item_ledger/`)

Материализованный стек остатков и резервов: движения регистра 1С → `stock_ledger_entry` → `stock_bin`; резервы `reservation_entry/event/coverage`. Включается флагом `STOCK_SOURCE` (`legacy` по умолчанию — поведение прежнее). Read-API — `routers/item_ledger.py`. Подробно: `item_ledger.md`, блокеры: `tech-debt.md`.

## Производственный и закупочный контуры

- Production-control (`services/production_control_*`): журнал заказов производства, выпуск («Произвести»), заявки на перемещение материалов (пул остатков участка, advisory-lock), резервы, печать; выгрузка в 1С — `one_c_*_export.py` (перемещение, сборка, сдельный наряд — «на основании» заказа).
- Purchase-control (`services/purchase_control_journal.py`): журнал заказов поставщику 1С + незаказанные MRP-закупки, экспорт `Document_ЗаказПоставщику`.
- Period plan (`services/period_plan_service.py`): страница «Период план», журнал исполнения. Контракт: `period_plan_target.md`.

## Синхронизация с 1С

`services/odata_client.py` + `*_sync.py` (номенклатура, спецификации, остатки, заказы, сотрудники…). `sync_orchestrator.py` — авто-синк со сдвигом по времени: воркер (`sync_worker.py`, отдельный контейнер) дёргает `tick()` ~раз в 2 мин, за тик не более одной OData-задачи, зависимости по порядку; состояние в JSON рядом с `odata_config.json`. Второй воркер — `reconcile_worker.py` (контейнер reconcile-worker).
