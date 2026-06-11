# Журнал закупок — план реализации

Постановка: **2026-06-11**. Цель: страница «Журнал закупок» по аналогии с «Журналом заказов на производство» (`/production-control`) — для отслеживания и контроля заказов поставщику. Журнал **полнофункциональный**: помимо просмотра — заказ MRP-потребностей в 1С прямо из журнала и синхронизация статусов/поступлений.

## Что уже есть (переиспользуем)

- БД: `supplier_orders` (номер, дата, `order_ref1c`, `supplier_id`, `order_state_key/name`, `deletion_mark`, сумма), `supplier_order_items` (`line_number`, `quantity`, `received_qty`, `remaining_qty`, `price`, `amount`, `delivery_date`), `suppliers` — `backend/app/models.py:525-568`.
- Синхронизация из 1С: `supplier_order_sync.py`, эндпоинт `POST /v1/sync/supplier-orders-odata` (включая состояния заказов и факт поступления `КоличествоПоступило`).
- Экспорт MRP-закупок в 1С: `POST /v1/plan/results/{run_id}/purchases/export-to-1c` (+ `SyncLink` для связи `planned_purchase` → `Document_ЗаказПоставщику`).
- Правила активности заказа: `deletion_mark = false`, статус не в {Новый заказ, Отменен, Завершен…} — см. `supplier_orders_check.md`.

Ничего нового в моделях БД не требуется. Журнал — read-модель поверх существующих таблиц.

## Backend

Новый роутер `backend/app/routers/purchase_control.py` + сервис `backend/app/services/purchase_control_journal.py` (по образцу `production_control_journal.list_journal`).

### `GET /v1/purchase-control/orders`

Строка журнала = строка заказа поставщику (`supplier_order_items` ⋈ `supplier_orders` ⋈ `suppliers` ⋈ `items`).

Параметры: `order_id`, `supplier_id`, `state` (статус заказа 1С), `line_status` (вычисляемый), `overdue_only`, `search` (номер/номенклатура/артикул), `date_from`/`date_to` (по `delivery_date`), `active_only` (по умолчанию true — правила из MRP), `sort_by` (`delivery_date` по умолчанию), `sort_dir`, `limit`/`offset`.

Поля строки: `order_id`, `order_number`, `order_date`, `order_ref1c`, `order_state_name`, `source` (`mrp`/`1c` — по наличию `SyncLink` на `planned_purchase`), `supplier_name`, `item_id/code/article/name`, `unit`, `quantity`, `received_qty`, `remaining_qty`, `delivery_date`, `overdue_days`, `line_status`, `price`, `amount`.

Вычисляемый `line_status`:
- `to_order` — MRP-потребность (`planned_purchase` последнего FIXED_SNAPSHOT-прогона) ещё не заказана в 1С (нет `SyncLink`);
- `received` — `remaining_qty ≤ 0`;
- `partial` — `received_qty > 0` и `remaining_qty > 0`;
- `overdue` — `remaining_qty > 0` и `delivery_date < today`;
- `expected` — `remaining_qty > 0`, срок не прошёл;
- `no_date` — `remaining_qty > 0`, `delivery_date` пуст;
- `closed` — заказ в финальном/исключённом статусе или `deletion_mark`.

Строки `to_order` — это незаказанные MRP-закупки последнего зафиксированного прогона (аналог внутренних заказов PRODPLAN в журнале производства): журнал показывает их вместе с заказами 1С, с чекбоксами для выгрузки.

### `GET /v1/purchase-control/orders/{order_id}` (карточка)
Шапка заказа + все строки — для боковой панели.

### `GET /v1/purchase-control/summary`
Счётчики для сводки: активных заказов, строк просрочено, ожидается за 7 дней, сумма в пути.

Тесты: pytest на `list_journal` (фильтры статусов, просрочка, active_only, пагинация) — по образцу тестов production_control.

## Frontend

Страница `frontend-erp-shell/src/ui/pages/PurchaseControlPage.tsx`, роут `/purchase-control`, пункт меню «Журнал закупок» рядом с «Журналом заказов». Структура — копия паттерна production-control (по правилам таблиц из `ai.md`):

- **commandBar** (полнофункциональный, по образцу `ProductionCommandBar`): «Обновить», **«Заказать в 1С»** (выгрузка выбранных `to_order`-строк через `POST /v1/plan/results/{run_id}/purchases/export-to-1c` с `purchase_ids`), «Синхронизировать с 1С» (вызов `POST /v1/sync/supplier-orders-odata` с сохранёнными настройками), «CSV», сводка (к заказу / просрочено / ожидается на неделе).
- **columnFilterTable**: Поиск | Поставщик (dropdown) | Статус строки (dropdown) | Статус 1С (dropdown) | даты поставки | «Только активные» (по умолчанию вкл).
- **Колонки** (doctype `purchaseOrdersDoctype.ts`): Заказ (номер+дата, бейдж MRP/1С) | Поставщик | Номенклатура (растущая) | Заказано | Поступило | Осталось | Дата поставки | Просрочка (дн., красным) | Статус (пилюля) | Сумма.
- **Detail pane** (по образцу `ProductionDetailPane`): карточка заказа — поставщик, состояние 1С, все строки заказа, ссылка на источник в MRP (`planned_purchase` через `SyncLink` → `/mrp-runs/{run_id}?tab=purchases&purchase_id=…`).

## Интеграция

- Deep-link из журнала исполнения: для `planned_purchase` с `one_c_opened` вести на `/purchase-control?order_id=…` (сейчас ведёт в MRP) — обновить `workItemHref` в `PeriodPlanPage.tsx`.
- Из таблицы закупок MRP-результата — ссылка «открыть в журнале закупок» для уже выгруженных строк.

## Этапы

1. Бэк: сервис + роутер + pydantic-схемы + тесты.
2. Фронт: doctype, страница, фильтры, detail pane, роут и меню; lint/build/smoke.
3. Перелинковка (журнал исполнения, MRP-результат).
4. Опционально позже: сводный режим «по заказам» (группировка строк), уведомления о просрочке.

Действия в журнале: «Заказать в 1С» (выгрузка незаказанных MRP-потребностей), «Синхронизировать с 1С» (статусы и факт поступления). Факт поступления по-прежнему приходит из 1С (`КоличествоПоступило`), отдельного документа поступления в PRODPLAN нет.
