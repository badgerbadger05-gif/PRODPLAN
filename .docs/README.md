# PRODPLAN — краткая документация

Цель этой папки — **давать минимум актуального контекста**, чтобы ИИ мог быстро вносить изменения в проект.

Важно:

- `.docs/prodplan-deploy.md` — **не часть базы знаний**, это шпаргалка по деплою (не редактировать).

## Быстрый старт

Windows:
```cmd
start.bat
```

Linux/macOS:
```bash
./start.sh
```

Адреса:
- Frontend: http://localhost:9000
- Backend Swagger: http://localhost:8000/docs

Пересборка при изменении кода:
```cmd
rebuild.bat
```

## Ключевые файлы

- `ai.md` — как вести задачи с ИИ
- `architecture.md` — архитектура и зоны ответственности
- `api.md` — API
- `db_schema.md` — база данных (кратко)
- `odata.md` — интеграция с 1С/OData, включая синхронизацию сотрудников `Catalog_Сотрудники`
- `one_c_export_from_prodplan.md` — правила выгрузки документов в 1С, включая обязательное создание “на основании”
- `piecework_order_odata.md` — проверенный контракт `Document_СдельныйНаряд` и его основание от `Document_СборкаЗапасов`
- `production_orders_check.md` — учет активных заказов на производство 1С в MRP
- `supplier_orders_check.md` — целевая документация по учету заказов поставщику 1С в MRP
- `execution_journal_ux_plan.md` — план улучшения читаемости журнала исполнения
- `purchase_journal_plan.md` — план журнала закупок (аналог журнала заказов)
- `frontend_erp_shell_migration.md` — решение и правила переноса фронта в новый ERP-shell
- `period_plan_target.md` — контракт страницы «Период план» (жизненный цикл, матрица, MRP-снимок, журнал, хоткеи)
- `troubleshooting.md` — команды и диагностика
- `progress.md` — текущее состояние/решения/проблемы
- `workplan.md` — **план работ с чекбоксами** (что делать дальше, приоритеты)

## Матрица CI-команд

Перед мержем убедиться что основные шаги зелёные:

| Шаг | Директория | Команда | Что проверяет |
|---|---|---|---|
| Backend pytest | корень проекта | `python -m pytest tests/ -x -q` | 148 тестов сервисов/роутеров |
| Frontend lint | `frontend-erp-shell/` | `npm run lint` | ESLint flat-config (0 ошибок) |
| Frontend build | `frontend-erp-shell/` | `npm run build` | TypeScript + Vite (0 ошибок типов) |
| Frontend smoke | `frontend-erp-shell/` | `npm run smoke` | Playwright smoke по ERP-shell |
| Docker smoke | корень проекта | `docker compose up -d && curl -f http://localhost:8000/health` | Backend стартует в контейнере |

## Правила

1) **Один запрос — одно изменение.**
2) Любые изменения модели БД → миграция Alembic.
3) Контракты API не ломаем без явного решения.
4) Дочерние документы 1С из PRODPLAN создаём “на основании”; `Document_ЗаказНаПроизводство` и `Document_ЗаказПоставщику` из MRP являются первичными и создаются без основания.
