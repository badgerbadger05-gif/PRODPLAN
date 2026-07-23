# Item-ledger — материализованный стек остатков и резервов

Нормативные доки (вне репо): `/home/ivan/PRODPLAN/mrp-item-ledger-design.md` (дизайн),
`/home/ivan/PRODPLAN/item-ledger-view-contract.md` (контракт карточки номенклатуры).
Здесь — карта реализации на ветке `feat/item-ledger`.

## Назначение

Два леджера поверх 1С, только чтение из 1С (INV-1way):

- **Леджер-1 (физика).** Движения `AccumulationRegister_ЗапасыНаСкладах` тянутся
  pull-by-document (по регистратору) → append-only `stock_ledger_entry` →
  агрегат `stock_bin` (остаток по ключу item/склад/характеристика/организация).
  `Balance(...)` 1С больше не источник остатка, а только **сверка** (drift) и
  **anchor** (T0-срез при первичной загрузке).
- **Леджер-2 (резервы).** `reservation_entry` (резерв MRP-потребности) +
  `reservation_event` (append-only журнал open/amend/realize/…) +
  `reservation_coverage` (покрытие: frozen-пины supplier/wip + floating от
  redistribute). Ось `realization_mode`: `consume` (списание компонента) /
  `make` (выпуск). Всё **soft-only**: планирование ничего не блокирует.

## Схема (7 таблиц, миграции `20260721_01..03`)

`stock_ledger_entry`, `stock_bin`, `stock_recorder_pull` (очередь pull'ов с
retry/attempts), `stock_ledger_anchor` (T0-якорь), `reservation_entry`,
`reservation_event`, `reservation_coverage`. Модели — в конце `backend/app/models.py`.

## Код

- `backend/app/services/item_ledger/` — `config.py` (флаг), `physical.py`
  (ключи, running balance), `ingest.py` (pull-by-document, очередь, replace-by-recorder
  под advisory-lock), `reconcile.py` (Balance-сверка, anchor), `reservation.py`
  (чистая модель Reserve/Pin/Pool + redistribute, 3 прохода A→B→C),
  `reservation_ledger.py` (ORM-адаптер, матчинг SLE→резерв, событийный журнал).
- `backend/app/routers/item_ledger.py` — read-API `/api/v1/item-ledger/{item_id}/…`,
  5 эндпоинтов: `/position`, `/movements`, `/reservations`,
  `/reservations/{reservation_id}/events`, `/drift` (контракт — view-contract §1–§5).
- `backend/app/routers/item_ledger_admin.py` — админ-операции:
  `POST /api/v1/item-ledger/admin/seed` (сид якоря T0, см. «Запуск»).

## Феатюр-флаг

`STOCK_SOURCE` ∈ {`legacy` (default), `bin`} — env, читается на каждый вызов
(`item_ledger/config.py`). `legacy` — поведение байт-в-байт как до ветки
(`ItemWarehouseStock`); `bin` — остатки из `stock_bin`. Известные читатели мимо
флага и прочие блокеры — [`tech-debt.md`](tech-debt.md).

## Инкременты (коммиты ветки)

| Инк | Коммит | Что |
|---|---|---|
| 0 | `6a938e9f` | Пробник OData движений регистра (гейт формы ответа — пройден) |
| 1 | `58f7b4b0` | Единая схема двух леджеров (7 таблиц) + чистые функции |
| 2 | `81f9bea1` | Физический ingest pull-by-document |
| 3 | `14d1124f` | Balance-сверка (drift) + shadow-диагностика |
| 4 | `a9b3f77f` | Материализация резервов (чистый shadow) |
| 5 | `d9d0e4bc` | Переключение читателей остатков на леджер (под флагом) |
| 6 | `1b3cbf03` | Неттинг/дрейф MRP на субстрате леджера (под флагом) |
| 7 | `8d3d3fb1` | Read-API карточки номенклатуры для фронта |
| фикс | `e49bf561` | Стоп перезаказу при испарении заказа поставщику (оба пути) |

## Запуск

Порядок ввода леджера в работу (дизайн Прил. A §4):

1. **Сид якоря T0** — `POST /api/v1/item-ledger/admin/seed`.
   Снимает свежий 1С `/Balance`, сеет seed-SLE + `stock_bin` +
   `stock_ledger_anchor(T0)` на каждый ненулевой ключ. Параметры:
   - `dry_run` (default **true**) — только сводка (строк Balance, ключей,
     Σ количеств, сколько якорей будет создано), БД не трогается. Сначала
     всегда смотрим dry-run.
   - `force` (default false) — пере-сид по A §4.5: `stock_ledger_entry`
     удаляется целиком, якоря/бины сбрасываются, очередь пуллов очищается,
     сид заново от нового T0. Без `force` повторный сид при существующих
     якорях → **409** (идемпотентность).
   Рекомендация A §9 №3: сеять сразу после планового Balance-свипа, в
   нерабочее окно 1С, при пустой очереди пуллов (сводка показывает
   `inflight_pulls`). Anchor guard в любом случае отсекает пуллы с
   `posting_at <= T0` — двойного учёта с сидом не будет.
2. **Дренаж очереди пуллов** — уже встроен в `sync_orchestrator.tick()` и
   работает сам: `process_pending_pulls` дренится приоритетом перед
   OData-джобами; attempts = число НЕУДАЧНЫХ попыток (успех не инкрементирует),
   re-enqueue сбрасывает счётчик. Здоровье очереди — `pull_queue_health`
   в статусе синка (`pending / error_retryable / error_exhausted / ready`).
   Balance-сверка (inc3) продолжает страховать: ретраябельный error-пулл
   держит adjustment (in-flight), exhausted — не держит.
3. **Включение `STOCK_SOURCE=bin`** — ТОЛЬКО после закрытия блокеров из
   [`tech-debt.md`](tech-debt.md) и ≥1 недели наблюдения shadow-отчёта
   (расхождения ledger vs legacy).

## Статус

Shadow-режим: `STOCK_SOURCE` не выставлен ⇒ прод-поведение не изменено.
**Включать `bin` в прод НЕЛЬЗЯ**, пока не закрыты блокеры из
[`tech-debt.md`](tech-debt.md) (матчинг через SyncLink, unrealize при
replace-by-recorder, release при закрытии прогона, дрен очереди pull'ов,
характеристики в сверке и др.).
