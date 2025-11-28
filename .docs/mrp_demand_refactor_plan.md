
# План изменений логики расчёта потребности MRP

## 1. Цели изменений

1. Упростить и сделать предсказуемой логику расчёта потребности:
   - отказаться от смешанного режима daily/weekly и эвристики по разрывам плана;
   - считать потребность строго по фактически заполненным дням плана.
2. Корректно использовать буфер по видам производства (`ProductionResource.buffer_days`):
   - не только в расчёте размера партии запуска, но и во временном смещении потребности по BOM.
3. Согласовать фактическую реализацию с документацией и конфигурацией:
   - убрать неиспользуемые и вводящие в заблуждение флаги (`use_weekly` и пр.);
   - начать реально учитывать существующие настройки (в т.ч. буфер и include_wip).

---

## 2. Текущее состояние (кратко)

### 2.1. Режим daily/weekly и деление горизонта

Функция [`compute_gross_requirements()`](backend/app/services/planning_service.py:1048):

- Делит спрос на:
  - `gross_daily[item_id][date]`
  - `gross_weekly[item_id][date]`
- Решение, что считать «недельным», принимается эвристикой:
  - строится `present_days` (дни, где есть план),
  - ищется непрерывный от `d0` интервал `dcontig`,
  - всё, что позже `dcontig`, и при `weekly.enabled = True` попадает в weekly‑корзины.
- Параметр `weekly.enabled` берётся из:
  - снапшота конфигурации,
  - либо переопределяется полем `use_weekly` из запроса `/v1/plan/calc`.

Проблемы:
- Поведение зависит от случайных «дыр» в плане (разрывы по дням).
- `mps_daily_horizon_days` из конфигурации не используется.
- На фронте есть переключатель Weekly, который переписывает поведение только для одного прогона.

### 2.2. Расчёт потребности по BOM

В той же функции:

- Для каждой записи MPS вызывается рекурсивный `expand_bom(item_id, qty, is_weekly, bucket_date, ...)`.
- Дата потребности компонента (`bucket_date`) **равна дате родителя**.
- Временной сдвиг по уровням BOM (через буфер или технологическое время) не учитывается.

### 2.3. Использование буфера по видам производства

Класс [`OrderQuantityCalculator`](backend/app/services/order_quantity_calculator.py:1):

- Характеристика `buffer_days` берётся из [`ProductionResource.buffer_days`](backend/app/models.py:258) через связку:
  - `Specification.production_kind_id` → `ResourceProductionKind` → `ProductionResource`.
- Буфер используется в `_calculate_buffer_qty`:

  ```text
  buffer_qty = avg_daily_demand * buffer_days
  ```

- Затем `_normalize_qty_for_production` выбирает финальное количество к запуску с приоритетами:
  1. `item.optimal_batch`,
  2. `buffer_qty`,
  3. min_batch / multiple / rounding.

Проблема:
- Буфер влияет только на **размер партии** (qty), но не на **дату потребности**.

---

## 3. Удаление режима weekly/daily

### 3.1. Цель

Полностью отказаться от понятия weekly/daily в логике ядра:

- Считать потребность **только по фактически заполненным дням** в `ProductionPlanEntry.date`.
- Оставить возможность *отчётной* агрегации по неделям (по пятницам) при необходимости — на уровне UI или отчётов, а не ядра MRP.

### 3.2. Изменения во фронтенде

Файл [`frontend/src/pages/MRPRunsPage.vue`](frontend/src/pages/MRPRunsPage.vue:1):

1. Удалить runtime‑переключатель Weekly на панели запуска:

   - Убрать элемент:

     ```vue
     <q-toggle v-model="form.use_weekly" label="Weekly" dense />
     ```

   - Из объекта `form` удалить свойство `use_weekly`:

     ```ts
     const form = reactive({
       horizon_days: 90 as number,
       // use_weekly: true as boolean   // удалить
     })
     ```

2. Перестать передавать `use_weekly` в API:

   - В `onCalc`:

     ```ts
     const res = await startPlanningRun({
       horizon_days: form.horizon_days,
       // use_weekly: form.use_weekly,  // удалить
       started_by: 'ui'
     })
     ```

3. Обновить таблицу прогонов:

   - В колонках `columns` убрать колонку `use_weekly`:

     ```ts
     { name: 'use_weekly', label: 'Weekly', field: (r: any) => (r.use_weekly ? 'Да' : 'Нет'), ... }
     ```

4. Компонент сводки [`MRPSummaryCard.vue`](frontend/src/components/mrp/MRPSummaryCard.vue):

   - Удалить отображение `summary.run.use_weekly` (строку типа «Weekly: Да/Нет»).

5. Типы:

   - [`MRPSummaryRun.use_weekly`](frontend/src/types/mrp.ts:7) — пометить как deprecated или удалить, если не используется больше нигде на фронте.

### 3.3. Изменения в backend API

Файл [`backend/app/routers/plan.py`](backend/app/routers/plan.py:198):

1. Модель `CalcRequest`:

   - Удалить поле `use_weekly`:

     ```python
     class CalcRequest(BaseModel):
         horizon_days: Optional[int] = None
         # use_weekly: Optional[bool] = None
         config_overrides: Optional[Dict[str, Any]] = None
         started_by: Optional[str] = None
     ```

2. Эндпоинты `/v1/plan/calc`, `/v1/plan/calc_preview`, `/v1/plan/calc_gross`:

   - Перестать передавать `use_weekly` в `run_planning_run` / `compute_*`:
     - аргумент `use_weekly` удалить из вызовов и сигнатур.

### 3.4. Изменения в планировании (backend/services)

Файл [`backend/app/services/planning_service.py`](backend/app/services/planning_service.py:1):

1. Функция `_get_or_create_run`:

   - Удалить аргумент `use_weekly` и всю логику по `overrides["weekly"]["enabled"]`.
   - В `snapshot` больше не должно быть runtime‑переопределения weekly.

2. Модель `PlanningRun`:

   - Поле `use_weekly` в БД и модели можно:
     - на первом шаге оставить (для совместимости и миграции), но перестать использовать;
     - в дальнейшем — удалить отдельной миграцией, если оно действительно не нужно.

3. `compute_gross_requirements`:

   - Упростить логику без weekly:

     - убрать вычисление `dcontig`;
     - не вводить `weekly_enabled`;
     - избавиться от `gross_weekly`, оставить одну структуру `gross[item_id][date]`.

   - New behavior:

     ```text
     • Для каждой строки MPS:
       - берём фактическую дату r.date.date()
       - считаем это датой корзины (bucket_date)
       - разворачиваем BOM и добавляем потребность всех уровней на эти же даты
     ```

   - Если потребуется недельная агрегация — делать её на уровне отчётов/представлений, группируя по пятницам, но НЕ в ядре net‑расчёта.

4. `compute_planning_preview`:

   - Считать net только по одной структуре `gross[item_id][date]`.
   - Вернуть net в виде:
     - либо только `net.daily[item][date]`,
     - либо как `net` без деления на daily/weekly.

5. `build_planned_orders_and_purchases`:

   - Сейчас использует `net_requirements` как `{ "daily": {...}, "weekly": {...} }`.

   - После упрощения:
     - интерфейс можно упростить до одного словаря `{ item_id: { date: net_qty } }`.
     - `bucket_type` для `PlannedOrder`/`PlannedPurchase` можно:
       - либо всегда считать `"daily"`;
       - либо вообще убрать, если дальше нигде не нужен (отдельный шаг миграции).

6. Вся логика, завязанная на `bucket_type = 'weekly'`, должна быть удалена или упрощена до однородного дневного режима.

---

## 4. Использование буфера в датах потребности по BOM

### 4.1. Бизнес‑идея

Сдвиг потребности по BOM вниз по иерархии:

- Сейчас:
  - `need_date(child) = need_date(parent)`.
- Предлагается:
  - `need_date(child) = need_date(parent) - buffer_days(child)`,
  - где `buffer_days(child)` берётся из `ProductionResource.buffer_days` соответствующего вида производства компонента.

Смысл:

- Для деталей, обрабатываемых на участке с буфером, потребность фиксируется **на несколько дней раньше**,
- что приближает поведение к «запасу на N дней вперёд» не только по объёму, но и по времени.

### 4.2. Где брать buffer_days для компонента

Исходя из уже существующей логики в [`OrderQuantityCalculator._calculate_buffer_qty`](backend/app/services/order_quantity_calculator.py:218):

1. Для текущего компонента определить `production_kind_id`:

   - из `Specification.production_kind_id` (той спецификации, где компонент фигурирует).

2. Для `production_kind_id` найти ресурсы в `ResourceProductionKind`:

   - взять список `resource_id`, связанных с этим видом производства.

3. Из таблицы `ProductionResource`:

   - выбрать один ресурс (например, первый по id),
   - взять его `buffer_days` (если `None` или `0` → буфера нет).

### 4.3. Встраивание в expand_bom

План изменений для [`expand_bom`](backend/app/services/planning_service.py:1114):

1. Добавить в замыкание (или параметрами) доступ к:

   - `spec_by_id`: `spec_id → Specification`,
   - `resource_kind_cache`: `production_kind_id → [ResourceProductionKind]`,
   - `res_by_id`: `resource_id → ProductionResource`.

   Эти структуры уже формируются в `run_planning_run` (для `OrderQuantityCalculator` и построения этапов) — их нужно переиспользовать или создать аналогичный кэш на стадии gross‑расчёта.

2. При обработке компонента:

   ```python
   parent_date = bucket_date
   buffer_days = resolve_buffer_days_for_item(child_id)
   if buffer_days > 0:
       child_date = parent_date - timedelta(days=buffer_days)
   else:
       child_date = parent_date
   ```

   где `resolve_buffer_days_for_item` реализует шаги из п.4.2.

3. Вместо:

   ```python
   add_to_bucket(child_id, bucket_date, child_qty, is_weekly)
   expand_bom(child_id, child_qty, is_weekly, bucket_date, new_path, depth + 1)
   ```

   использовать:

   ```python
   add_to_bucket(child_id, child_date, child_qty, ...)
   expand_bom(child_id, child_qty, ..., child_date, new_path, depth + 1)
   ```

4. Ограничение по низу горизонта:

   - Если `child_date < d0`, можно:
     - либо обрезать до `d0` (вся потребность попадает в первый день горизонта),
     - либо оставить вне горизонта (не добавлять такую корзину) — зависит от принятой политики. Для простоты и предсказуемости безопаснее **обрезать до d0**.

### 4.4. Связь с OrderQuantityCalculator

- OrderQuantityCalculator уже использует буфер по видам производства в **объёме**.
- Новый сдвиг в `expand_bom` добавит использование того же буфера во **времени**.
- При этом:
  - нет противоречий — мы не меняем сигнатуры калькулятора,
  - логика demand limit по горизонту остаётся прежней, просто часть потребностей «переезжает» на более ранние даты.

---

## 5. Использование include_wip и других настроек конфигурации

В рамках рефакторинга логично одновременно:

1. **Подключить `toggles.include_wip` в `compute_planning_preview`:**

   - Сейчас WIP всегда учитывается при неттинге.
   - Нужно читать `include_wip` из `snapshot["toggles"]["include_wip"]`:
     - если `False` — не добавлять WIP к `available_qty`,
     - если `True` — поведение остаётся как сейчас.

2. **Перестать использовать `weekly.enabled` в расчёте потребности:**

   - После отказа от разделения daily/weekly в ядре конфигурационный блок `weekly` становится не нужен в MRP‑ядре,
   - При необходимости его можно оставить для отчётных/визуальных функций или удалить в отдельной миграции.

---

## 6. Пошаговый план реализации

1. **Frontend:**
   - Удалить runtime‑переключатель Weekly на странице прогонов.
   - Перестать передавать `use_weekly` в `/v1/plan/calc`.
   - Удалить/скрыть отображение `use_weekly` в таблице прогонов и сводке.

2. **Backend API:**
   - Обновить `CalcRequest` (убрать `use_weekly`).
   - Обновить эндпоинты `/calc`, `/calc_preview`, `/calc_gross`:
     - удалить параметр `use_weekly` из сигнатур функций сервиса.

3. **Core MRP (planning_service):**
   - Упростить `compute_gross_requirements`:
     - убрать разделение daily/weekly,
     - отказаться от `dcontig` и эвристики по разрывам плана,
     - использовать только фактические даты MPS.
   - Обновить `compute_planning_preview` и `build_planned_orders_and_purchases` соответственно (одна структура net, один тип корзин).
   - Подключить `toggles.include_wip` в неттинге.

4. **Буфер в датах потребности:**
   - Добавить в `compute_gross_requirements` или на уровне `run_planning_run` кэши:
     - `spec_by_id`,
     - `resource_kind_cache`,
     - `res_by_id`.
   - Реализовать функцию `resolve_buffer_days_for_item(item_id)` с учётом вида производства.
   - Изменить `expand_bom`, чтобы:
     - сдвигать `child_need_date` на `buffer_days`,
     - передавать сдвинутую дату вниз по иерархии.

5. **Очистка от weekly‑следов:**
   - По результатам:
     - удалить использование `weekly.enabled` из снапшота конфигурации в сервисах MRP,
     - рассмотреть удаление поля `use_weekly` из `PlanningRun` и блоков `weekly` из плановой конфигурации (отдельной миграцией).

---

Этот план описывает все ключевые изменения: полное удаление режима weekly/daily из ядра расчёта, перевод буфера по видам производства в разряд временного смещения потребности по BOM и синхронизацию поведения с фактическим форматом плана (по дням, с неделями на пятницу).</result>
</attempt_completion>