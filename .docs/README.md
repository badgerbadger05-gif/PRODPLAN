# PRODPLAN — краткая документация

Цель папки — **минимум актуального контекста** для быстрой работы с проектом.
Канонические правила работы агентов — `AGENTS.md` в корне репозитория.

## Быстрый старт

Windows: `start.bat` · Linux/macOS: `./start.sh` · пересборка: `rebuild.bat`

Адреса: Frontend http://localhost:9000 · Backend Swagger http://localhost:8000/docs

## Живые документы

Справочники:
- `architecture.md` — компоненты и контуры (MRP, DBR, item-ledger, production/purchase-control, sync)
- `api.md` — группы роутеров backend
- `db_schema.md` — таблицы БД по доменам
- `tech-debt.md` — известные мины и долги (несущий `create_all`, блокеры item-ledger, DBR)
- `progress.md` — текущее состояние, короткий журнал сессий
- `prodplan-deploy.md` — единственная актуальная шпаргалка по живому деплою на `mtzdock.lan`
- `troubleshooting.md` — команды и диагностика

Контуры / фичи:
- `item_ledger.md` — леджер остатков и резервов (`stock_ledger_entry`/`stock_bin`, флаг `STOCK_SOURCE`)
- `dbr_parallel_module_roadmap.md` — модуль «Барабан + Питатели» (DBR); seed-данные — `dbr_seed/`
- `paint_weld_chain_logic.md` — связка «окраска ↔ сварка» в журнале заказов
- `period_plan_target.md` — контракт страницы «Период план»
- `production_orders_check.md` — учёт активных заказов на производство 1С в MRP
- `supplier_orders_check.md` — учёт заказов поставщику 1С в MRP

Интеграция с 1С:
- `odata.md` — синхронизация из 1С (OData), включая `Catalog_Сотрудники`
- `one_c_export_from_prodplan.md` — правила выгрузки документов в 1С («на основании» и первичные)
- `piecework_order_odata.md` — контракт `Document_СдельныйНаряд`

Архив: `archive/` — исполненные планы, код-ревью, старые анализы и дневник
`archive/progress_2026H1.md`. Аналогично `docs/archive/` для пользовательских доков.

## Матрица CI-команд

| Шаг | Директория | Команда | Что проверяет |
|---|---|---|---|
| Backend pytest | корень проекта | `./.venv/bin/python -m pytest tests/ -x -q` | 1175 тестов сервисов/роутеров |
| Frontend lint | `frontend-erp-shell/` | `npm run lint` | ESLint flat-config (0 ошибок) |
| Frontend build | `frontend-erp-shell/` | `npm run build` | TypeScript + Vite (0 ошибок типов) |
| Frontend smoke | `frontend-erp-shell/` | `npm run smoke` | Playwright smoke по ERP-shell |
| Docker smoke | корень проекта | `docker compose up -d && curl -f http://localhost:8000/health` | Backend стартует в контейнере |

## Правила

1) **Один запрос — одно изменение.**
2) Любые изменения модели БД → миграция Alembic + обновить `db_schema.md`.
3) Контракты API не ломаем без явного решения.
4) Дочерние документы 1С из PRODPLAN создаём «на основании»; `Document_ЗаказНаПроизводство` и `Document_ЗаказПоставщику` из MRP — первичные, без основания.
