# PRODPLAN — ERP frontend roadmap

Статус: рабочий план реализации. React остаётся основным frontend-стеком.

## Неподвижные принципы

1. Существующий UX основных страниц — продуктовый актив. Миграция на общий runtime не меняет рабочие сценарии, плотность таблиц, русские подписи и последовательность действий без отдельного решения.
2. MRP, DBR, периодный план и сложные производственные экраны остаются custom pages. Doctype/runtime применяется к журналам, спискам, карточкам и общим механизмам.
3. Backend — единственный источник прав, аудита и ledger-данных. Frontend только отображает разрешённые действия и не рассчитывает проводки.
4. API-типы генерируются из `docs/api/openapi.json`; ручное дублирование контрактных схем запрещено.

## Целевая frontend-архитектура

```text
ERP shell
├── resource registry (routes, navigation, capabilities, permissions)
├── auth/session + resource/action/record/field gates
├── typed API data provider
├── list controller
│   ├── filters / sorting / pagination
│   ├── active row / selection / bulk actions
│   └── personal and shared saved views
├── dense configurable table
├── detail/card renderer
├── dialog host
├── audit timeline
└── custom pages (MRP / DBR / Period Plan / production workflows)
```

Архитектурные референсы, не зависимости для полной миграции:

- React-admin Data Provider, list controller, saved queries and configurable grids;
- Refine resource/data/access-control providers;
- Frappe Desk workspaces, permissions and keyboard shell;
- Odoo personal/shared/default saved filters.

## Выполнено

- OpenAPI → TypeScript generation и проверка дрейфа в CI.
- Doctype types/runtime: columns, filters, actions, list/detail state, pagination, dynamic list metadata.
- Разделение list/detail/action loading и защита от устаревших detail-ответов.
- Permission checks на экране, кнопке и внутри action runner; action scope enforcement.
- Явный submit и debounce режимы поиска.
- Вложенные detail tables и extension slots.
- `MRP Runs` мигрирован на Doctype runtime с характеристическими тестами.
- `Transfer Requests` мигрирован с сохранением двухстрочных ячеек, column filters, деталей и команд.
- Resource registry для shell и route-level lazy loading.
- Frontend CI: generated API drift, lint, tests, production build.
- Lint приведён к нулю; устранён незакрытый timer в тестах Period Plan.

## Следующие вертикальные срезы

### 1. Purchase Control

- typed list metadata: summary, phases, suppliers, states, latest run;
- URL/deep-link filters;
- selection predicate и select-all только для допустимых строк;
- server sorting;
- CSV, sync, propose/export actions;
- сохранение существующих summary chips и detail pane;
- характеристические тесты.

### 2. Auth and RBAC shell

После публикации backend endpoints:

- `/auth/login`, `/auth/me`, `/auth/logout`;
- единый bearer/cookie transport в `lib/api.ts`;
- navigation filtering через resource registry;
- resource/action/record/field gates;
- отдельные permissions для всех записей в 1С;
- тесты 401/403 и ролевые E2E.

До появления backend-auth мутирующие Doctype-экраны используют явно объявленный transitional subject; это не считается завершённой безопасностью.

### 3. Saved views and configurable dense tables

- filter + sort + visible columns + density;
- personal/shared/default scopes;
- URL serialization для ссылок;
- backend storage contract для shared views;
- column chooser и восстановление представления.

### 4. Ledger UI

После публикации schemas/endpoints:

- item/pool card;
- immutable postings journal;
- source document and calculation provenance;
- projection balances;
- reconciliation cycles and discrepancies;
- reversal chain;
- audit timeline with actor, time, action, diff, source and correlation id.

### 5. Production hardening

- typed dialog registry with fallback, focus trap, Escape and return focus;
- keyboard/command shell;
- accessible table navigation and `aria-sort`/`aria-selected`/live regions;
- hermetic Playwright tests with mocked routes;
- separate backend-contract E2E;
- visual regression for user-approved core pages;
- bundle budgets per route.

## Definition of Done

Frontend foundation можно считать полноценной только когда:

- все новые журналы создаются через registry + data provider + list controller;
- критичные существующие журналы имеют characterization/visual tests;
- роли проверяются frontend и backend независимо;
- saved views, bulk actions, audit timeline и keyboard shell работают единообразно;
- lint, generated API check, unit/component tests, build and E2E являются обязательными CI-гейтами;
- ledger screens показывают происхождение данных и никогда не редактируют производные проекции напрямую.
