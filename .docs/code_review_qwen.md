# PRODPLAN — Сводный отчёт по ревью кода

**Дата:** 2026-06-14 (выходной, плановое ревью)

---

## О проекте

**PRODPLAN** — система планирования производства (MRP) с интеграцией с 1C:УНФ через OData. Рассчитывает потребность в материалах, формирует заказы на производство/закупку/доработку и экспортирует документы обратно в 1C.

**Стек:**

| Слой | Технология | Версия |
|------|-----------|--------|
| Backend | Python / FastAPI | 3.11 / >=0.68 |
| ORM | SQLAlchemy | >=1.4 (2.x-style imports) |
| Миграции БД | Alembic | >=1.7 (48 миграций, head: `20260611_01`) |
| База данных | PostgreSQL | 15 |
| DTO / валидация | Pydantic | >=1.8 (v2-style `ConfigDict`) |
| ASGI-сервер | Uvicorn | >=0.15 (4 workers) |
| Frontend | React / TypeScript | 19.1 / 5.8.3 |
| Сборка | Vite | 6.3.5 |
| Роутер | react-router-dom | 7.15.1 |
| Линтинг | ESLint | 9.39.4 (flat config) |
| E2E / Smoke | Playwright | 1.60.0 |
| Веб-сервер (prod) | nginx | 1.27-alpine |
| Контейнеризация | Docker + Docker Compose | Compose v2 |
| Интеграция с 1C | OData (1C:УНФ) | через собственный OData-клиент |
| Тестирование | pytest | >=7.0 (315 тестов, in-memory SQLite) |

**Масштаб:** ~60 сервисных модулей Python, 12 роутеров, 40+ таблиц БД, 48 миграций Alembic, 14 страниц фронтенда, 315 тестов pytest.

---

## Архитектура

### Docker-стек (5 сервисов)

| Сервис | Образ | Порты (local / test) | Назначение |
|--------|-------|---------------------|------------|
| `db` | postgres:15 | 55432:5432 / 55433:5432 | PostgreSQL |
| `backend` | Custom (Python 3.11-slim) | 8000:8000 / 8010:8000 | FastAPI REST API |
| `frontend` | Custom (nginx:1.27-alpine) | 9000:80 / 9010:80 | React SPA через nginx |
| `sync-worker` | Same as backend | — | Авто-синхронизация с 1C каждые ~2 мин |
| `reconcile-worker` | Same as backend | — | MRP-пересчёт каждые 3 часа |

### API (12 роутеров)

| Роутер | Префикс | Назначение |
|--------|---------|------------|
| `items.py` | `/api/items` | CRUD номенклатуры |
| `sync.py` | `/api/v1/sync` | Синхронизация с 1C OData |
| `odata.py` | `/api/v1/odata` | Сырые OData-операции |
| `plan.py` | `/api/v1/plan` | MRP-запуски, периодические планы, reconciliation |
| `nomenclature.py` | `/api/v1/nomenclature` | Поиск номенклатуры |
| `stages.py` | `/api/v1/stages` | Этапы производства |
| `specification.py` | `/api/v1/specification` | Дерево спецификаций (BOM) |
| `resources.py` | `/api/v1/resources` | Ресурсы/цеха |
| `production_control.py` | `/api/v1/production-control` | Журнал производства, экспорт в 1C |
| `purchase_control.py` | `/api/v1/purchase-control` | Журнал закупок |
| `workshop_binding_review.py` | `/api/v1/workshop-binding-review` | Привязка цехов к складам |
| `production_control_settings.py` | `/api/v1/production-control/settings` | Настройки привязок |

### Фронтенд (14 маршрутов)

| Маршрут | Страница | Назначение |
|---------|----------|------------|
| `/` | HomePage | Дашборд |
| `/period-plan` | PeriodPlanPage | Периодическое планирование (item × week) |
| `/mrp-runs` | MrpRunsPage | История MRP-запусков |
| `/mrp-runs/:runId` | MrpResultPage | Результаты MRP-запуска |
| `/production-control` | ProductionControlPage | Журнал производства |
| `/purchase-control` | PurchaseControlPage | Журнал закупок |
| `/transfer-requests` | TransferRequestsPage | Заявки на перемещение |
| `/production-report-week` | ProductionReportWeekPage | Еженедельный отчёт |
| `/resources` | ResourcesPage | Ресурсы/цеха |
| `/workshop-binding-review` | WorkshopBindingReviewPage | Привязка цехов к складам |
| `/stage-distribution` | StageDistributionPage | Распределение по этапам |
| `/specification` | SpecificationPage | Просмотр спецификаций |
| `/sync` | SyncPage | Синхронизация с 1C |

---

## 🔴 Критические замечания (4)

### 1. Нет аутентификации/авторизации

**Файл:** `backend/app/main.py`

Все API-эндпоинты полностью открыты. Нет auth middleware, нет dependency injection для идентификации пользователя, нет API-ключей, нет JWT. Любой, кто имеет сетевой доступ, может:
- Запускать синхронизацию с 1C
- Создавать/удалять производственные заказы
- Экспортировать документы в 1C (записывающие операции)
- Читать все производственные данные

CORS ограничивает только по origin, что не является реальной защитой.

### 2. Пароли и секреты в открытом виде

**Файлы:**
- `backend/app/services/odata_config.py` — `save_odata_config()` сохраняет username/password/token как plaintext JSON
- `docker-compose.yml` строки 20, 42 — `POSTGRES_PASSWORD: password`
- `backend/app/database.py` строка 9 — fallback `postgresql://user:password@localhost:5432/prodplan`

Учётные данные 1C хранятся как plaintext JSON в `config/odata_config.json`. Пароль БД `password` захардкожен в docker-compose.yml. Если env-переменная `DATABASE_URL` не установлена, приложение подключается с известными учётными данными.

### 3. `iter_by_guid` определена вне класса

**Файл:** `backend/app/services/odata_client.py` ~строка 370

Функция `iter_by_guid` определена на уровне модуля (не внутри класса `OData1CClient`), но использует `self` как первый параметр. При вызове упадёт с `NameError: name 'self' is not defined`. Это либо мёртвый код, либо потерянный метод класса.

### 4. `create_all()` при каждом старте конфликтует с Alembic

**Файл:** `backend/app/main.py` строка 23

```python
Base.metadata.create_all(bind=engine)
```

Выполняется при каждом импорте/старте и создаёт таблицы в обход Alembic-миграций. Может создать таблицы в несогласованном состоянии (без индексов, ограничений или столбцов, которые добавляют миграции). Делает систему миграций ненадёжной.

---

## 🟠 Серьёзные замечания (10)

### 5. Ссылка на несуществующую модель `ODataConfig`

**Файл:** `backend/app/routers/sync.py` строка 310

```python
config = db.query(models.ODataConfig).first()
```

Модель `ODataConfig` не определена в `models.py`. Эндпоинт `/debug/production-order-states` упадёт с `AttributeError` при вызове.

### 6. `.dict()` вместо `.model_dump()` (Pydantic v1 → v2)

**Файлы:**
- `backend/app/services/item_service.py` строки 22, 32
- `backend/app/routers/odata.py` строки 51, 58, 83, 132

Проект использует Pydantic v2 (есть `model_config = ConfigDict(from_attributes=True)`), но вызывает `.dict()`, который deprecated в v2. Будут предупреждения. Нужно заменить на `.model_dump()`.

### 7. `datetime.utcnow()` deprecated (Python 3.12+)

**Файлы:** 11+ файлов, включая:
- `backend/app/services/planning_service.py` (строки 574, 4140)
- `backend/app/services/one_c_export_common.py` (строка 124)
- `backend/app/services/production_control_production_flow.py` (строка 357)
- `backend/app/services/one_c_stock_transfer_export.py` (строка 505)

Заменить на `datetime.now(timezone.utc)`.

### 8. 460× `except Exception` — тихое проглатывание ошибок

По всему коду 460 конструкций `except Exception`. Многие используются как silent swallow:

```python
except Exception:
    pass
```

Это скрывает баги, затрудняет отладку и может маскировать повреждение данных. Нужна конкретика (например, `except (ValueError, TypeError)`) или хотя бы логирование исключения.

### 9. OData-пароли проходят через браузер

**Файлы:** `frontend-erp-shell/src/domain/sync.ts`, `frontend-erp-shell/src/ui/pages/SyncPage.tsx`

Тип `ODataConfig` включает `username` и `password`. SyncPage рендерит форму, где оператор вводит пароль 1C, и этот пароль отправляется из браузера на бэкенд при каждом sync-действии. Пароль путешествует по сети и может быть перехвачен, если HTTPS не настроен, или утечь через браузерные расширения, dev tools или дампы памяти.

**Рекомендация:** Бэкенд должен хранить учётные данные серверно и не гонять их через фронтенд.

### 10. `document.write()` с серверным HTML — XSS-вектор

**Файл:** `frontend-erp-shell/src/ui/pages/ProductionControlPage.tsx`, функция `renderRouteSheets()`

```typescript
printWindow.document.open()
printWindow.document.write(html)
printWindow.document.close()
```

HTML-ответ от `/api/v1/production-control/route-sheets/print` записывается напрямую в popup-окно через `document.write()`. Если бэкенд когда-либо отразит пользовательский контент без санитизации — это XSS в контексте popup.

### 11. Контейнеры работают от root

**Файл:** `backend/Dockerfile`

Нет директивы `USER` — процесс работает от root внутри контейнера. При компрометации контейнера атакующий получает root-доступ.

### 12. Debug-эндпоинты без защиты

**Файлы:**
- `backend/app/routers/specification.py` — `/debug`
- `backend/app/routers/sync.py` — `/debug/production-order-states`

Отдают внутреннее состояние БД, логику разрешения спецификаций и состояния заказов 1C без какого-либо контроля доступа.

### 13. Нет управления транзакциями

Большинство сервисных функций принимают `db: Session`, но не управляют транзакциями сами. `db.commit()` вызывается то в сервисах, то в роутерах, то не вызывается вовсе:
- `plan.py` ~строка 480: `bulk_upsert_plan_entries()` без `db.commit()`
- `plan.py` ~строка 510: `db.commit()` вызывается явно после `bulk_upsert_fact()`
- `items.py`: `create_item()` вызывает `db.commit()` внутри сервиса

Непредсказуемое поведение: некоторые операции могут не быть закоммичены или коммитятся в неожиданный момент.

### 14. Захардкоженные GUID 1C

**Файл:** `backend/app/services/one_c_export_common.py` строки 12-14

```python
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"
DEFAULT_ORGANIZATION_REF1C = "c78bcd0e-81f0-11ee-9ce5-9ee51454587f"
DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C = "c74ea54c-d1b2-11ef-9e01-9ee51454587f"
```

Захардкожены для конкретной базы 1C. При переключении на другой экземпляр 1C данные будут записаны в неправильную организацию/структурную единицу.

### 15. Produce flow без полного отката

**Файл:** `frontend-erp-shell/src/ui/pages/ProductionControlPage.tsx`, функция `submitProduce()`

Производственный flow — многошаговая транзакция: (1) корректировка количества, (2) создание material issues, (3) экспорт в 1C, (4) запись производства локально, (5) экспорт производства в 1C, (6) экспорт сдельного наряда. При сбое на шагах 3-6 уже созданные material issues НЕ откатываются. Остаются документы-сироты.

---

## 🟡 Замечания средней важности (17)

### Архитектура и размер файлов

| Файл | Строк | Проблема |
|------|-------|----------|
| `planning_service.py` | 4144 | Весь MRP-движок в одном файле |
| `models.py` | 1033 | 40+ моделей в одном файле |
| `schemas.py` | ~648 | Все Pydantic-схемы в одном файле |
| `plan.py` | 1693 | ~50 эндпоинтов в одном роутере |
| `specification.py` | 1247 | Дерево спецификаций + debug |
| `PeriodPlanPage.tsx` | 1722 | Матрица + журнал в одном компоненте |
| `ProductionControlPage.tsx` | 1114 | ~40 `useState`, вся логика производства |
| `styles.css` | 1736 | Без CSS-модулей, без скоупа, без BEM |

### Качество кода

- **57 вызовов `print()`** вместо `logging` в сервисном слое (`production_order_sync.py` ~20, `supplier_order_sync.py` ~6, `odata_client.py` 3 на запрос)
- **`_to_float()` дублируется** в 6+ файлах (`production_control_common.py`, `period_plan_service.py`, `production_report_service.py`, `specification.py`, `nomenclature_search.py`, `forced_orders.py`)
- **`_date_to_iso()` дублируется** с разными реализациями
- **`ForecastShift`** — 3 копии компонента (`PeriodPlanPage.tsx`, `MrpResultPage.tsx`, `ProductionOrdersTable.tsx`)
- **Нет React Error Boundary** — любое падение компонента при рендере = белый экран
- **N+1 запросы** в дереве спецификаций (`_children_for_item()` → `_has_children()` → `_resolve_spec_id_for_item_id()`)
- **Семантический поиск на MD5** (`nomenclature_search.py`) — `_generate_embedding` использует `hashlib.md5` для 4-элементного float-вектора. Не настоящая семантика, комментарий «Это временное решение для демонстрации»

### Тесты

- **315 тестов** покрывают сервисы, но **9 из 11 роутеров не покрыты** тестами
- **Нет unit-тестов фронтенда** — только 1 smoke-тест Playwright
- **`conftest.py`** использует глобальный engine — возможна утечка состояния между тестами

### Инфраструктура и конфигурация

- **Нет rate limiting** на API (особенно опасно для sync-эндпоинтов — можно перегрузить 1C)
- **Нет конфигурации connection pool** — `create_engine(DATABASE_URL, pool_pre_ping=True)` без `pool_size`, `max_overflow`, `pool_recycle`
- **Экспортные эндпоинты** грузят до 100 000 строк в память (`limit=100000` в `plan.py`)
- **Build-инструменты** (`vite`, `@vitejs/plugin-react`, `typescript`) в `dependencies` вместо `devDependencies`
- **`HashRouter`** вместо `BrowserRouter` — `HashRouter` обычно для статического хостинга, а тут есть бэкенд
- **CSV-экспорт** не экранирует переводы строк в значениях ячеек (`PurchaseControlPage.tsx`)
- **OData filter injection** — `get_all()` передаёт `filter_query` напрямую в OData URL без санитизации (`odata_client.py`)

---

## 🟢 Что сделано хорошо

- ✅ **Чистая структура доменных типов** — 10 TypeScript-модулей в `domain/`, 10 сервисов в `services/`
- ✅ **TypeScript strict mode** — 0 ошибок сборки
- ✅ **315 pytest-тестов** на бизнес-логику — солидное покрытие MRP, экспорта в 1C, production control
- ✅ **Alembic-миграции** — 48 миграций, версионирование БД в порядке
- ✅ **Разделение на compose-профили** — local-dev и test/prod раздельно
- ✅ **Background workers** на stdlib — лёгкие, без лишних зависимостей
- ✅ **Документация** — 26 файлов в `.docs/`, runbook деплоя, AI-правила
- ✅ **Idempotency** через `sync_link` — защита от дублей при экспорте в 1C
- ✅ **ESLint flat config** — современный подход, 0 ошибок
- ✅ **Multi-stage Dockerfile** для фронтенда — node:22-alpine builder → nginx:1.27-alpine

---

## 📊 Итоговая сводка

| Категория | 🔴 Крит. | 🟠 Серьёз. | 🟡 Средне | 🟢 Хорошо |
|-----------|---------|-----------|----------|----------|
| Безопасность | 2 | 4 | 2 | — |
| Корректность кода | 2 | 4 | 3 | — |
| Архитектура | — | 1 | 6 | 3 |
| Тесты | — | — | 3 | 2 |
| Инфраструктура | — | 1 | 3 | 3 |
| **Итого** | **4** | **10** | **17** | **8** |

---

## 🎯 Рекомендуемый порядок действий

### Фаза 1 — Безопасность (1-2 дня)

1. Добавить хотя бы базовую HTTP Basic Auth или API-key middleware
2. Вынести все пароли/секреты в env-переменные, убрать из кода и git
3. Закрыть debug-эндпоинты или удалить их
4. Добавить `USER nonroot` в Dockerfile бэкенда

### Фаза 2 — Исправление багов (1 день)

5. Починить `iter_by_guid` (перенести в класс или удалить)
6. Удалить ссылку на `models.ODataConfig` в `sync.py`
7. Заменить `.dict()` → `.model_dump()` для совместимости с Pydantic v2
8. Заменить `datetime.utcnow()` → `datetime.now(timezone.utc)`
9. Убрать `create_all()` из `main.py` (пусть Alembic управляет схемой)

### Фаза 3 — Качество (по выходным, постепенно)

10. Разбить `planning_service.py` на модули по доменам
11. Разделить `models.py` / `schemas.py` на пакеты
12. Заменить `print()` на `logging`
13. Добавить React Error Boundary
14. Покрыть роутеры тестами
15. Настроить connection pool (`pool_size`, `max_overflow`, `pool_recycle`)
16. Добавить rate limiting на sync-эндпоинты
