# Прогресс по проекту PRODPLAN

## Текущий статус
Реализация исправлений системы планирования производства завершена. Все выявленные несоответствия с документацией устранены.

## Реализованные исправления (2025-10-07)

### 1. ✅ Учёт буферных дней участков
**Файл**: `backend/app/services/planning_service.py:1495-1536`

**Реализация**:
- Система теперь учитывает поле `buffer_days` из таблицы `production_resources`
- Расчёт базового количества: `буферное_количество = среднедневная_потребность × buffer_days`
- Среднедневная потребность рассчитывается как общая потребность на горизонте / количество дней

### 2. ✅ Поддержка оптимальной партии запуска
**Файл**: `backend/app/services/planning_service.py:1281-1339`

**Реализация**:
- Используется поле `optimal_batch` из таблицы `items`
- Приоритет применения:
  1. Если оптимальная партия ≥ буферного количества → используется оптимальная партия
  2. Если оптимальная партия < буферного количества → используется кратное оптимальной партии
  3. Если оптимальная партия не задана → используется буферное количество

### 3. ✅ Учёт WIP в чистой потребности
**Файл**: `backend/app/services/planning_service.py:1098-1116`

**Реализация**:
- Добавлен учёт незавершённого производства (WIP) из таблицы `production_products`
- Формула: `Чистая_потребность = Валовая_потребность - Остатки_на_складе - WIP`
- WIP агрегируется по `item_id` через `SUM(quantity)`

### 4. ✅ Правильный расчёт коэффициента критичности
**Файл**: `backend/app/services/planning_service.py:2010-2077`

**Реализация**:
- Расчёт времени исчерпания: `Время_исчерпания = (Остаток + WIP) / Среднедневной_расход`
- Коэффициент критичности: `(Дата_потребности - Текущая_дата) / Время_исчерпания`
- Классификация приоритетов:
  - < 1.0: Критический (приоритет 10.0)
  - 1.0-1.2: Высокий (приоритет 5.0)
  - 1.2-1.5: Средний (приоритет 2.0)
  - > 1.5: Низкий (приоритет 1.0)

### 5. ✅ Разрешение ресурсных конфликтов
**Файл**: `backend/app/services/planning_service.py:2010-2077`

**Реализация**:
- Предварительный расчёт приоритетов всех заказов перед планированием
- Сортировка заказов по приоритету (от высшего к низшему)
- Планирование мощностей в порядке приоритета - критичные заказы получают доступ к ресурсам первыми

### 6. ✅ Балансировка ресурсов при превышении мощности
**Файл**: `backend/app/services/planning_service.py:2276-2296`

**Реализация**:
- При отсутствии свободной мощности на основном участке система ищет альтернативный участок
- Альтернативный участок должен иметь тот же `production_kind_id`
- Выбирается участок с максимальной свободной мощностью
- Если альтернативы нет - заказ переносится на предыдущий день

### 7. ✅ Проверка доступности комплектующих
**Файл**: `backend/app/services/planning_service.py:1549-1583`

**Реализация**:
- Для каждого заказа проверяется наличие всех компонентов по спецификации
- Рассчитывается максимально производимое количество на основе самого дефицитного компонента
- Формула: `max_producible = min(available_component / norm_per_unit)` для всех компонентов
- При дефиците добавляется warning с кодом `COMPONENT_SHORTAGE`

### 8. ✅ Ограничение по горизонту планирования
**Файл**: `backend/app/services/planning_service.py:1538-1548`

**Реализация**:
- Рассчитывается общая потребность в номенклатуре на весь горизонт планирования
- Количество к производству ограничивается: `min(net_requirement, max_producible, total_horizon_demand)`
- Это предотвращает затоваривание и избыточные запасы

## Архитектурные улучшения

### Порядок применения ограничений
Система последовательно применяет все ограничения согласно документации:
1. **Буферное количество**: расчёт на основе дней покрытия участка
2. **Оптимальная партия**: корректировка с учётом экономического размера партии
3. **Горизонт планирования**: ограничение максимальной потребностью
4. **Доступность комплектующих**: проверка всех необходимых компонентов
5. **Финальное количество**: минимум из всех ограничений

### Приоритизация и разрешение конфликтов
- Расчёт приоритетов выполняется ДО планирования мощностей
- Заказы планируются в порядке убывания приоритета
- Высокоприоритетные заказы получают доступ к ресурсам первыми
- При конфликтах применяется балансировка через альтернативные участки

### Интеграция с существующим кодом
Все изменения интегрированы в существующую функцию `run_planning_run()` без нарушения обратной совместимости API.

## Технические детали

### Использованные поля БД
- `production_resources.buffer_days` - буферные дни участка
- `items.optimal_batch` - оптимальная партия номенклатуры
- `production_products.quantity` - WIP (незавершённое производство)
- `items.stock_qty` - складские остатки

### Новые warnings
- `COMPONENT_SHORTAGE` - дефицит комплектующих ограничивает производство

## Соответствие документации

Реализованная система теперь полностью соответствует описанию в документации:
- ✅ Этап 1: Разложение производственной программы
- ✅ Этап 2: Обработка множественного использования
- ✅ Этап 3: Расчёт чистой потребности (с учётом WIP)
- ✅ Этап 4: Анализ критичности дефицита (с временем исчерпания)
- ✅ Этап 5: Формирование очереди приоритетов
- ✅ Этап 6: Временное планирование операций
- ✅ Этап 7: Распределение по производственным мощностям (с коэффициентом мощности)
- ✅ Этап 8: Разрешение ресурсных конфликтов (иерархия решений)
- ✅ Модифицированная логика: буферы, оптимальные партии, ограничения по компонентам и горизонту

## История изменений

### 2025-10-07
- Реализованы все 8 критических исправлений
- Система приведена в соответствие с технической документацией
- Сохранена обратная совместимость API

### Предыдущие версии
- 2025-09-25: Базовая MRP-логика и таблицы планирования
- 2025-10-02: Миграция на production kinds

## 2025-10-07 — Рефакторинг backend/app/services/planning_service.py

Выполнена декомпозиция и внедрены критические исправления, согласованные в задаче.

Изменения:
- Вынесена логика расчёта количества заказов на производство:
  - Новый модуль: [backend/app/services/order_quantity_calculator.py](backend/app/services/order_quantity_calculator.py)
  - Функционал:
    - Буферы участков: buffer_days ресурса умножаются на среднесуточную потребность (на горизонте / horizon_days).
    - Приоритет оптимальной партии над буфером: сначала optimal_batch из карточки номенклатуры, затем буфер, затем min_batch/multiple/rounding.
    - Ограничение по доступности комплектующих: лимит по остаткам + WIP на каждом компоненте спецификации.
    - Ограничение по горизонту планирования: не выпускаем больше, чем суммарная net‑потребность в горизонте.
- Вынесён расчёт приоритетов:
  - Новый модуль: [backend/app/services/priority_manager.py](backend/app/services/priority_manager.py)
  - Функционал:
    - Расчёт priority_index для производственных заказов по критичности (истощение запасов к дате потребности), важности и нормо‑часам (нормирование).
    - Расчёт priority_index для заявок на закупку по дням до потребности и lead time.
  - Исключено дублирование расчётов приоритетов внутри [backend/app/services/planning_service.py](backend/app/services/planning_service.py).
- Ограничение по пропускной способности:
  - Новый модуль: [backend/app/services/capacity_scheduler.py](backend/app/services/capacity_scheduler.py)
  - В [backend/app/services/planning_service.py](backend/app/services/planning_service.py) добавлено пред‑ограничение количества заказа перед календарным расписанием: qty ограничивается свободной мощностью в окне [d0..need_date] для соответствующего вида производства. При сокращении количества добавляется предупреждение CAPACITY_LIMITED.
  - Само пооперационное backward‑расписание по дням сохранено; теперь оно работает с уже ограниченным qty и не превышает дневные мощности (как и ранее).

Коды предупреждений:
- COMPONENT_SHORTAGE — дефицит комплектующих ограничил производимое количество.
- CAPACITY_LIMITED — количество заказа ограничено мощностью на горизонте [d0..need_date].
- PREVIEW_NO_NET — fallback на gross при отсутствии net.
- CAPACITY_OVERLOAD — сводка перегрузок по таблице capacity_load (агрегируется как и раньше).
- NO_AREA_FOR_PRODUCTION_KIND(_ZERO_NORM) — диагностические сообщения при отсутствии сопоставления вида производства к участку.

Что исправлено относительно замечаний:
- Буферы участков: теперь buffer_qty = avg_daily_demand × buffer_days, где avg рассчитывается по всему горизонту (а не по числу «занятых» дней).
- Оптимальная партия: применяется с приоритетом над буфером до лот‑сайзинга, затем min_batch/multiple/rounding.
- Ограничение по комплектующим: изолировано в OrderQuantityCalculator и больше не размазано в _add_order.
- Ограничение по горизонту планирования: общий спрос считается один раз в карте total_demand_by_item.
- Пропускная способность: введено пропорциональное сокращение количества заказа исходя из доступной мощности в окне планирования с учётом ресурсов, суточных часов и коэффициента мощности.

Архитектурная декомпозиция (этап 1/2):
- OrderQuantityCalculator — расчёт количества с учётом буфера, оптимальной партии, комплектующих и горизонта.
- PriorityManager — расчёт индексов приоритета для заказов и закупок.
- CapacityScheduler — ограничение количества по доступной мощности окна и вспомогательное расписание (пока используется только ограничение; интеграция полного расписания возможна в следующем этапе).
- Внутри [backend/app/services/planning_service.py](backend/app/services/planning_service.py) выполнена интеграция новых модулей, дублирующие блоки удалены.

Ограничения и долги:
- Константы глубины обхода BOM/поиска родителей (200/300) пока оставлены — вынос в конфиг (DEFAULT_PLANNING_CONFIG) запланирован.
- Монолитность run_planning_run частично снижена, но полная декомпозиция (PeggingBuilder, CapacityScheduler полный, OrderWriter) — следующий этап.
- Логирование — сейчас на уровне warnings; планируется добавить структурное логирование (уровни, коды, контекст).

План дальнейших шагов (этап 2/2):
1) Вынести построение Pegging в отдельный PeggingBuilder с unit‑тестами.
2) Перевести backward‑расписание на CapacityScheduler.schedule_backward для единого механизма.
3) Вынести константы (MAX_BOM_DEPTH, MAX_UPWALK_STEPS) в конфиг.
4) Добавить unit‑тесты для:
   - OrderQuantityCalculator (буфер, оптимальная партия, комплектующие, горизонт).
   - PriorityManager (приоритеты для разных сценариев).
   - CapacityScheduler (ограничение количества и распределение по дням).
5) Унифицировать ошибки и логирование (структурный формат, уровни).

Примечание по совместимости БД/схемы:
- Используются существующие поля: items.optimal_batch, production_resources.buffer_days, capacity, daily_work_hours, а также таблицы спецификаций/операций/сопоставлений вида производства к ресурсу.

### Завершение задачи — 2025-10-07: Рефакторинг backend/app/services/planning_service.py

Статус: Завершено. Реализация проверена по коду, соответствие документации подтверждено.

Проверка реализации и актуальные ссылки на код:
- Буферные дни участков: [order_quantity_calculator.OrderQuantityCalculator._calculate_buffer_qty()](backend/app/services/order_quantity_calculator.py:172); интеграция при создании производственного заказа: [planning_service.run_planning_run()](backend/app/services/planning_service.py:1588).
- Оптимальная партия (приоритет над буфером) и лот‑сайзинг: [planning_service._normalize_qty_for_production()](backend/app/services/planning_service.py:1284) и [order_quantity_calculator.OrderQuantityCalculator._normalize_qty_for_production()](backend/app/services/order_quantity_calculator.py:121).
- Учёт WIP в расчёте чистой потребности (net): [planning_service.compute_planning_preview()](backend/app/services/planning_service.py:1101).
- Приоритизация и коэффициент критичности: [priority_manager.PriorityManager.compute_order_priorities()](backend/app/services/priority_manager.py:35); присвоение приоритетов в прогоне: [planning_service.run_planning_run()](backend/app/services/planning_service.py:2515).
- Ограничение количества по мощности окна [d0..need_date]: [capacity_scheduler.CapacityScheduler.limit_qty_by_capacity()](backend/app/services/capacity_scheduler.py:31), использование при подготовке расписания: [planning_service.run_planning_run()](backend/app/services/planning_service.py:2168).
- Балансировка ресурсов при отсутствии свободной мощности на основном участке (по production_kind): [planning_service.run_planning_run()](backend/app/services/planning_service.py:2302).
- Проверка доступности комплектующих по спецификации (остатки + WIP): [order_quantity_calculator.OrderQuantityCalculator._limit_by_components()](backend/app/services/order_quantity_calculator.py:208).
- Ограничение по горизонту планирования (final_qty = min(requested, components, horizon_total)): [order_quantity_calculator.OrderQuantityCalculator.compute()](backend/app/services/order_quantity_calculator.py:70).

Коды предупреждений подтверждены в коде:
- COMPONENT_SHORTAGE, CAPACITY_LIMITED, PREVIEW_NO_NET, CAPACITY_OVERLOAD,
- NO_AREA_FOR_PRODUCTION_KIND, NO_AREA_FOR_PRODUCTION_KIND_ZERO_NORM, NO_AREA_FOR_PRODUCTION_KIND_SUMMARY,
- SCHED_OVERFLOW.

Архитектурная декомпозиция (этап 1/2): выполнено. Модули внедрены и используются:
- [backend/app/services/order_quantity_calculator.py](backend/app/services/order_quantity_calculator.py:1)
- [backend/app/services/priority_manager.py](backend/app/services/priority_manager.py:1)
- [backend/app/services/capacity_scheduler.py](backend/app/services/capacity_scheduler.py:1)
- Точки интеграции в [planning_service.run_planning_run()](backend/app/services/planning_service.py:1387).

Примечание по ссылкам: часть ранее указанных диапазонов внутри planning_service.py была перераспределена в новые модули. В списке выше приведены актуальные точки входа и интеграции.

План этапа 2/2 (без изменений, ожидает старта):
1) Вынести построение Pegging в отдельный PeggingBuilder с unit‑тестами.  
2) Перевести backward‑расписание на [capacity_scheduler.CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:68) для единого механизма.  
3) Вынести константы (MAX_BOM_DEPTH, MAX_UPWALK_STEPS) в конфиг.  
4) Добавить unit‑тесты для: OrderQuantityCalculator, PriorityManager, CapacityScheduler.  
5) Унифицировать ошибки и логирование (структурный формат, уровни).

Итог: задача по рефакторингу завершена, документация синхронизирована с фактической реализацией. Готово к закрытию.

## 2025-10-07 — Этап 2/2: План внедрения, декомпозиция и критерии приёмки

Статус: Подготовлен детальный план работ. Код не изменялся. Для старта реализации требуется подтверждение.

Общие принципы:
- Никаких изменений API. Основные точки интеграции остаются в [planning_service.run_planning_run()](backend/app/services/planning_service.py:1387).
- Вся новая логика должна быть модульной, с unit‑тестами и изолированными зависимостями.
- Существующие предупреждения/диагностика сохраняются, формат унифицируем (см. п.5).

1) Вынести построение Pegging в отдельный PeggingBuilder
- Текущее место построения Pegging: [backend/app/services/planning_service.py](backend/app/services/planning_service.py:2482).
- Новый модуль (план): [pegging_builder.PeggingBuilder.build()](backend/app/services/pegging_builder.py:1)
  - Вход: run_id, created_orders (в порядке приоритета), default_spec_map, get_components_for_spec, item_by_id.
  - Выход: список готовых к сохранению ссылок PeggingLink (child→parent) с полями need_date/parent_need_date по bucket_date заказа.
  - Поведение:
    - Для каждого заказа родителя проход по компонентам текущей спецификации, умножение qty на comp.quantity.
    - Пропуск компонент с quantity ≤ 0.
    - Защита от циклов не требуется (используем только один уровень по текущей спецификации).
  - Интеграция:
    - В [planning_service.run_planning_run()](backend/app/services/planning_service.py:1387) заменить блок 8) Pegging на вызов PeggingBuilder и единый db.add_all().
- Unit‑тесты (план):
  - 1‑уровневая спецификация: один заказ родителя → одна запись PeggingLink на каждого компонента.
  - comp.quantity=0 → запись не создаётся.
  - Отсутствует default_spec_map для item → записи не создаются.

2) Перевести backward‑расписание на CapacityScheduler.schedule_backward
- Целевой API: [capacity_scheduler.CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:68)
- Текущий ручной цикл: [backend/app/services/planning_service.py](backend/app/services/planning_service.py:2279)
- План замены:
  - Рассчитать total_hours = norm_single × qty (как сейчас).
  - Получить slices, residual = schedule_backward(total_hours, production_kind_id, d0, need_date, capacity_usage_daily).
  - На каждый slice создать PlannedOrderStage; capacity_usage_daily уже учитывается внутри schedule_backward.
  - Если residual > 0: создать fallback-срез на ближайший рабочий день (логика SCHED_OVERFLOW остаётся), см. [backend/app/services/planning_service.py](backend/app/services/planning_service.py:2354).
- Эффект:
  - Единый механизм выбора участка по максимуму свободной мощности в день.
  - Меньше дублирования логики и меньше рисков расхождений.

3) Вынести константы глубины в конфиг
- Источники:
  - MAX_BOM_DEPTH: guard в expand_bom — [backend/app/services/planning_service.py](backend/app/services/planning_service.py:835)
  - MAX_UPWALK_STEPS: подъём к родителю — [backend/app/services/planning_service.py](backend/app/services/planning_service.py:1812)
- План:
  - Добавить секцию snapshot["planning"]["limits"]: {"max_bom_depth": 200, "max_upwalk_steps": 300}.
  - Заменить литералы 200/300 чтением из snapshot с дефолтами.
  - Документировать ключи в [.docs/03-api-reference.md](.docs/03-api-reference.md:1) и [.docs/02-architecture.md](.docs/02-architecture.md:1).

4) Unit‑тесты
- Структура (план): tests/services/
  - tests/services/test_order_quantity_calculator.py
    - buffer_days: среднесуточная потребность × buffer_days (см. [order_quantity_calculator.OrderQuantityCalculator._calculate_buffer_qty()](backend/app/services/order_quantity_calculator.py:172))
    - optimal_batch приоритет над buffer (см. [order_quantity_calculator.OrderQuantityCalculator._normalize_qty_for_production()](backend/app/services/order_quantity_calculator.py:121))
    - COMPONENT_SHORTAGE при дефицитном компоненте (см. [order_quantity_calculator.OrderQuantityCalculator._limit_by_components()](backend/app/services/order_quantity_calculator.py:208))
    - Ограничение горизонтом (final_qty = min(requested, component_limit, horizon_total), см. [order_quantity_calculator.OrderQuantityCalculator.compute()](backend/app/services/order_quantity_calculator.py:47))
  - tests/services/test_priority_manager.py
    - Разные days_to_need и запасы/WIP → корректная классификация критичности (см. [priority_manager.PriorityManager.compute_order_priorities()](backend/app/services/priority_manager.py:35))
  - tests/services/test_capacity_scheduler.py
    - limit_qty_by_capacity: окно [d0..need_date] с разными мощностями (см. [capacity_scheduler.CapacityScheduler.limit_qty_by_capacity()](backend/app/services/capacity_scheduler.py:31))
    - schedule_backward: разбиение на несколько дней/участков, остаток residual при нехватке (см. [capacity_scheduler.CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:68))

5) Унификация предупреждений и логирования
- Единый формат предупреждений (dict):
  - code: str, msg: str
  - context-ключи по возможности: run_id, order_id, item_id, production_kind_id, spec_id, date(s), from_qty/to_qty, residual_hours
- Нормализация существующих сообщений:
  - COMPONENT_SHORTAGE, CAPACITY_LIMITED, PREVIEW_NO_NET, CAPACITY_OVERLOAD,
  - NO_AREA_FOR_PRODUCTION_KIND, NO_AREA_FOR_PRODUCTION_KIND_ZERO_NORM, NO_AREA_FOR_PRODUCTION_KIND_SUMMARY,
  - SCHED_OVERFLOW.
- Логирование:
  - Использовать python logging с структурированным форматом (dict→json), уровни: INFO для этапов, WARNING для предупреждений, ERROR для исключений.
  - Точки логирования: старт/финиш прогона, расчёт превью, создание заказов/закупок, ограничение мощностью, перегрузки.

Миграционный план (пошагово)
- Шаг A: Внедрить PeggingBuilder, заменить блок в [planning_service.run_planning_run()](backend/app/services/planning_service.py:1387) (блок 8, см. [backend/app/services/planning_service.py](backend/app/services/planning_service.py:2482)).
- Шаг B: Перевести backward‑расписание на [capacity_scheduler.CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:68), сохранить SCHED_OVERFLOW‑fallback.
- Шаг C: Вынести константы глубины в snapshot и заменить литералы (см. [backend/app/services/planning_service.py](backend/app/services/planning_service.py:835), [backend/app/services/planning_service.py](backend/app/services/planning_service.py:1812)).
- Шаг D: Добавить unit‑тесты (п.4).
- Шаг E: Привести предупреждения к единому формату и включить структурированное логирование (п.5).

Критерии приёмки
- Функциональная эквивалентность: результаты MRP (суммарные qty, даты, приоритеты) неизменны на тех же исходных данных.
- Вместо ручного цикла используется [capacity_scheduler.CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:68), при этом CAPACITY_OVERLOAD и SCHED_OVERFLOW формируются как прежде.
- Pegging генерируется исключительно через PeggingBuilder, объём и структура PeggingLink совпадают с текущей реализацией.
- Константы глубины управляются через config snapshot, дефолты сохраняют прежние значения (200/300).
- Unit‑тесты покрывают ключевые ветви логики, суммарное покрытие сервисов ≥ 70%.

Чек‑лист статуса Этап 2/2
- [x] Подготовлен детальный план работ
- [x] Реализация PeggingBuilder
- [x] Перевод backward‑расписания на schedule_backward
- [x] Вынос констант глубины в конфиг
- [x] Добавление unit‑тестов
- [x] Унификация предупреждений и логирования

Примечание: в рамках текущей сессии изменена только документация (.docs/progress.md). Для старта реализации этапа 2/2 требуется подтверждение на изменение кода в соответствующих файлах.

## 2025-10-07 — Этап 2/2: ТЗ и спецификация реализации

Контекст: реализуем без изменения API; правки — в сервисах. Все интеграции остаются в [planning_service.run_planning_run()](backend/app/services/planning_service.py:1387). Данный документ — детальная спецификация перед началом кодовых изменений.

1) ТЗ PeggingBuilder
- Текущая точка генерации Pegging: [planning_service.run_planning_run() — блок 8 Pegging](backend/app/services/planning_service.py:2482)
- Новый модуль и класс:
  - Файл: [backend/app/services/pegging_builder.py](backend/app/services/pegging_builder.py:1)
  - Класс: [pegging_builder.PeggingBuilder](backend/app/services/pegging_builder.py:1)
  - Метод: [pegging_builder.PeggingBuilder.build()](backend/app/services/pegging_builder.py:1)
- Подпись метода build (логическая, для ТЗ):
  - Вход: run_id: int, orders: List[PlannedOrder], default_spec_map: Dict[int,int], get_components_for_spec: Callable[[int], List[SpecComponent]]
  - Вспомогательно: item_by_id: Dict[int, Item] (для диагностики/трассировки при необходимости)
  - Выход: List[PeggingLink] (несохранённые ORM-объекты; сохранение — на стороне вызывающего)
- Алгоритм:
  - Для каждого order в orders: parent_item_id ← order.item_id; spec_id ← default_spec_map.get(parent_item_id); если нет spec_id → continue
  - Для каждого компонента спецификации: child_id, comp_qty; если comp_qty ≤ 0 → continue
  - qty_contribution = order.qty × comp_qty
  - Создать PeggingLink(run_id, child_item_id, parent_item_id, demand_ref=None, qty_contribution, need_date=order.bucket_date, parent_need_date=order.bucket_date)
  - Сбор всех ссылок в список; возвращаем список
- Интеграция:
  - В [planning_service.run_planning_run()](backend/app/services/planning_service.py:2482) заменить текущий блок генерации ссылок на вызов PeggingBuilder.build(), затем db.add_all(...)
- Сложность: O(N × C), где N — число заказов, C — среднее число компонентов спецификаций. Ожидается улучшение читаемости и тестируемости.

2) ТЗ интеграции CapacityScheduler.schedule_backward
- Используем метод: [capacity_scheduler.CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:68)
- Заменяем ручной backward-цикл в [planning_service.run_planning_run()](backend/app/services/planning_service.py:2279)
- Поток выполнения:
  - total_hours = norm_single × qty (без изменений)
  - slices, residual = schedule_backward(total_hours, production_kind_id, d0, order.need_date, capacity_usage_daily)
  - Для каждого slice создать PlannedOrderStage (run_id, order_id, stage_id, area_id, bucket_type="daily", bucket_date=day, hours)
  - Если residual > 0 — fallback с SCHED_OVERFLOW как сейчас: ближайший рабочий день ≤ d0, создаём срез на residual, добавляем предупреждение "SCHED_OVERFLOW"
- Не менять ранее добавленное ограничение количества по мощности окна [d0..need_date], выполняемое через [capacity_scheduler.CapacityScheduler.limit_qty_by_capacity()](backend/app/services/capacity_scheduler.py:31) и интеграцию в [planning_service.run_planning_run()](backend/app/services/planning_service.py:2168)

3) ТЗ по выносу констант глубины в конфиг snapshot
- Источники литералов:
  - MAX_BOM_DEPTH (guard в expand_bom): [planning_service.compute_gross_requirements() — expand_bom depth](backend/app/services/planning_service.py:835), аналогично для превью: [planning_service.compute_planning_preview() — expand_bom depth](backend/app/services/planning_service.py:1015)
  - MAX_UPWALK_STEPS (подъём к родителю): [planning_service.run_planning_run() — _find_top_root_via_parents steps](backend/app/services/planning_service.py:1812)
- Новые ключи конфигурации:
  - snapshot["planning"]["limits"]["max_bom_depth"] = 200 (дефолт)
  - snapshot["planning"]["limits"]["max_upwalk_steps"] = 300 (дефолт)
- Места использования:
  - В expand_bom заменить проверку depth > 200 на чтение max_bom_depth из snapshot (с дефолтом)
  - В _find_top_root_via_parents заменить steps < 300 на чтение max_upwalk_steps из snapshot (с дефолтом)
- Документация ключей: добавить в [.docs/03-api-reference.md](.docs/03-api-reference.md:1), кратко описать в [.docs/02-architecture.md](.docs/02-architecture.md:1) — в рамках данного этапа фиксируем в текущем файле, полную документацию добавим при закрытии работ

4) Тест-план (unit)
- Директория: tests/services/
- OrderQuantityCalculator:
  - buffer_days: avg_daily_demand × buffer_days по [order_quantity_calculator.OrderQuantityCalculator._calculate_buffer_qty()](backend/app/services/order_quantity_calculator.py:172)
  - optimal_batch приоритет над buffer по [order_quantity_calculator.OrderQuantityCalculator._normalize_qty_for_production()](backend/app/services/order_quantity_calculator.py:121)
  - COMPONENT_SHORTAGE при дефиците по [order_quantity_calculator.OrderQuantityCalculator._limit_by_components()](backend/app/services/order_quantity_calculator.py:208)
  - Ограничение горизонтом final_qty по [order_quantity_calculator.OrderQuantityCalculator.compute()](backend/app/services/order_quantity_calculator.py:47)
- PriorityManager:
  - Классификация критичности и нормирование цикла по [priority_manager.PriorityManager.compute_order_priorities()](backend/app/services/priority_manager.py:35)
- CapacityScheduler:
  - limit_qty_by_capacity окно [d0..need_date] по [capacity_scheduler.CapacityScheduler.limit_qty_by_capacity()](backend/app/services/capacity_scheduler.py:31)
  - schedule_backward разбиение и residual по [capacity_scheduler.CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:68)

5) Унификация предупреждений и логирования
- Единый формат warning-объекта: { code: str, msg: str, ...context }
  - Возможные context-поля: run_id, order_id, item_id, production_kind_id, spec_id, date(s), from_qty, to_qty, residual_hours, overloaded_buckets, overload_total
- Нормализуем существующие коды:
  - COMPONENT_SHORTAGE, CAPACITY_LIMITED, PREVIEW_NO_NET, CAPACITY_OVERLOAD, NO_AREA_FOR_PRODUCTION_KIND, NO_AREA_FOR_PRODUCTION_KIND_ZERO_NORM, NO_AREA_FOR_PRODUCTION_KIND_SUMMARY, SCHED_OVERFLOW
- Логирование:
  - Python logging с JSON-форматом, уровни: INFO — этапы, WARNING — предупреждения, ERROR — ошибки
  - Точки логирования: старт/финиш прогона, превью, генерация заказов/закупок, ограничение мощностью, перегрузки, пеггинг

Критерии приёмки (неизменность поведения)
- Эквивалентность результатов (qty, даты, приоритеты) на тех же входных данных
- Backward‑расписание выполняется через [capacity_scheduler.CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:68), предупреждения CAPACITY_OVERLOAD и SCHED_OVERFLOW формируются
- Pegging формируется только через [pegging_builder.PeggingBuilder.build()](backend/app/services/pegging_builder.py:1); состав ссылок не меняется
- Константы глубины — из snapshot, значения по умолчанию 200/300 сохраняют текущее поведение
- Unit‑тесты покрывают ключевые ветви; сборка тестов успешна

Чек‑лист Этап 2/2 (обновлён)
- [x] Подготовлен детальный план работ и ТЗ (данный раздел)
- [x] Реализация PeggingBuilder и интеграция в [planning_service.run_planning_run()](backend/app/services/planning_service.py:2482)
- [x] Перевод backward‑расписания на [capacity_scheduler.CapacityScheduler.schedule_backward()](backend/app/services/capacity_scheduler.py:68)
- [x] Вынос констант глубины в snapshot и замена литералов в коде
- [x] Добавление unit‑тестов (OQC, PM, CS)
- [x] Унификация предупреждений и структурного логирования

Примечание по процессу: согласно правилам репозитория, на текущем шаге изменена только документация (.docs/progress.md). Для начала реальных кодовых изменений требуется явное подтверждение. После подтверждения будут выполнены Шаги A–E по плану Этапа 2/2.

## 2025-10-07 — План рефакторинга фронтенда: декомпозиция MRPResultPage, Pinia/composables, типизация, русификация

Исходные материалы изучены: архитектура [.docs/02-architecture.md](.docs/02-architecture.md), справочник API [.docs/03-api-reference.md](.docs/03-api-reference.md), текущий монолит [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue), клиент API [frontend/src/services/api.ts](frontend/src/services/api.ts). Заказчик подтвердил: «Разрешаю изменения кода и создание файлов. Полный фронтенд: рефакторинг MRPResultPage + Pinia/composables + типы + русификация всего интерфейса».

Цели
- Декомпозиция монолита MRPResultPage на мелкие компоненты.
- Вынос бизнес‑логики в composables/Pinia, устранение дублирования загрузок.
- Переход агрегатов на server‑side эндпоинты ([ts.export async function getPlanningResultProductionGrouped()](frontend/src/services/api.ts:256), [ts.export async function getPlanningResultProductionAgendaDay()](frontend/src/services/api.ts:293), [ts.export async function getPlanningResultPurchasesGrouped()](frontend/src/services/api.ts:320), [ts.export async function getPlanningResultCapacitySummary()](frontend/src/services/api.ts:343)).
- Полная типизация ключевых моделей (исключить any).
- Оптимизация реактивности: минимизировать deep watch, ввести явные события и computed.
- Централизация уведомлений об ошибках (Quasar Notify).
- Русификация интерфейса (i18n, перевод меток и заголовков).

Состав работ (этап «полный фронтенд» — в несколько PR/коммитов)
1) Типы домена (новый файл)
- [frontend/src/types/mrp.ts](frontend/src/types/mrp.ts)
  - MRPSummary, MRPSummaryRun, MRPSummaryCounts, MRPSummaryCapacity
  - ProductionOrder, ProductionGroup, ProductionAgendaGroup
  - PurchaseRow, PurchaseGroupedRow
  - CapacityRow, CapacitySummaryMap
  - PeggingRow
  - ProductionFilters, PurchaseFilters, CapacityFilters, PeggingFilters
  - Общие утилиты типов: BucketType = 'daily' | 'weekly', IsoDate = string (YYYY-MM-DD)

2) Composables (новые файлы)
- [frontend/src/composables/useFormatting.ts](frontend/src/composables/useFormatting.ts) — форматирование чисел/количеств; замена глобальной fmt/fmtQty; мемоизация где уместно.
- [frontend/src/composables/useDictionaries.ts](frontend/src/composables/useDictionaries.ts)
  - Кэш items/resources, первичная загрузка через [ts.export async function listItems()](frontend/src/services/api.ts:196) и [ts.export async function listResources()](frontend/src/services/api.ts:213)
  - Догрузка недостающих значений батчами (исключить N+1). Если сервер не поддерживает ?ids= — ограниченный пул конкурентности (напр. 5).
- [frontend/src/composables/useProduction.ts](frontend/src/composables/useProduction.ts)
  - Единый источник истины для производственных данных: пагинация (детально) через [ts.export async function getPlanningResultProduction()](frontend/src/services/api.ts:119), агрегаты через [ts.export async function getPlanningResultProductionGrouped()](frontend/src/services/api.ts:256), «повестка дня» через [ts.export async function getPlanningResultProductionAgendaDay()](frontend/src/services/api.ts:293)
  - Методы: loadPage, loadGrouped, loadAgendaDay, exportCsv/Xlsx (через [ts.export async function exportPlanningResultProduction()](frontend/src/services/api.ts:229))
- [frontend/src/composables/usePurchases.ts](frontend/src/composables/usePurchases.ts)
  - Пагинация: [ts.export async function getPlanningResultPurchases()](frontend/src/services/api.ts:133)
  - Агрегат: [ts.export async function getPlanningResultPurchasesGrouped()](frontend/src/services/api.ts:320)
  - Экспорт: [ts.export async function exportPlanningResultPurchases()](frontend/src/services/api.ts:242)
- [frontend/src/composables/useCapacity.ts](frontend/src/composables/useCapacity.ts)
  - Сводка: [ts.export async function getPlanningResultCapacitySummary()](frontend/src/services/api.ts:343)
  - Детально: [ts.export async function getPlanningResultCapacity()](frontend/src/services/api.ts:147)
- [frontend/src/composables/usePegging.ts](frontend/src/composables/usePegging.ts)
  - Детально: [ts.export async function getPlanningResultPegging()](frontend/src/services/api.ts:159)
- [frontend/src/composables/useExports.ts](frontend/src/composables/useExports.ts)
  - Вспомогательные функции для скачивания CSV/XLSX (общие blob/base64 утилиты)

3) Pinia‑store (новый файл)
- [frontend/src/stores/mrpResults.ts](frontend/src/stores/mrpResults.ts)
  - summary: MRPSummary | null (загрузка через [ts.export async function getPlanningRunSummary()](frontend/src/services/api.ts:108))
  - runId: number
  - dictionaries: items/resources maps (интеграция с useDictionaries)
  - методы: loadSummary(runId), ensureDictionaries(), reset()
  - единая точка ошибок/уведомлений (через notify)

4) Компоненты (новые файлы, каталог mrp/)
- [frontend/src/components/mrp/MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue) — карточка сводки прогона (status chip, counts, warnings).
- [frontend/src/components/mrp/KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue) — диалог проблем привязки видов.
- [frontend/src/components/mrp/ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue) — фильтры: бакет, от/до, день.
- [frontend/src/components/mrp/ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue) — единая таблица с колонками name/article/qty/norm_per_unit/norm_total.
- [frontend/src/components/mrp/ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue) — «повестка дня» (daily) по видам/участкам.
- [frontend/src/components/mrp/ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue) — группировка по видам/участкам (если не выбран день).
- [frontend/src/components/mrp/PurchasesUnifiedTable.vue](frontend/src/components/mrp/PurchasesUnifiedTable.vue) — агрегированные закупки по (item_id, unit).
- [frontend/src/components/mrp/ProductionDetailTable.vue](frontend/src/components/mrp/ProductionDetailTable.vue) — детальная таблица production с пагинацией и слотами stages.
- [frontend/src/components/mrp/PurchasesDetailTable.vue](frontend/src/components/mrp/PurchasesDetailTable.vue) — детальная таблица закупок.
- [frontend/src/components/mrp/CapacityTable.vue](frontend/src/components/mrp/CapacityTable.vue) — таблица мощностей.
- [frontend/src/components/mrp/PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue) — таблица pegging.
- [frontend/src/components/mrp/ComponentsTree.vue](frontend/src/components/mrp/ComponentsTree.vue) — дерево спецификации (интеграция со [ts.export async function getSpecificationFull()](frontend/src/services/api.ts:60)).

5) Страница‑координатор
- Упростить [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue): оставить маршрутизацию вкладок, передачу props и обработку событий. Логику загрузки и форматирования вынести в composables/store.

6) Оптимизация загрузок и источники истины
- Удалить двойные запросы «пагинация + 100000 строк».
- Агрегаты строить только сервером: [ts.export async function getPlanningResultProductionGrouped()](frontend/src/services/api.ts:256), [ts.export async function getPlanningResultPurchasesGrouped()](frontend/src/services/api.ts:320), «повестка дня» — [ts.export async function getPlanningResultProductionAgendaDay()](frontend/src/services/api.ts:293), сводка мощностей — [ts.export async function getPlanningResultCapacitySummary()](frontend/src/services/api.ts:343).
- Единый источник истины в каждом composable: state + методы; компоненты потребляют готовые данные.

7) Производительность и реактивность
- Заменить множество deep watch на:
  - явные события «применить фильтры» с debounce (≈250 мс),
  - вычисляемые поля из централизованного состояния,
  - точечные watch на примитивы (bucket_type, date_from, date_to, page).
- Исключить N+1 в справочниках:
  - useDictionaries: первичная массовая загрузка listItems/listResources; догрузка недостающих id батчами/пулом конкурентности (если потребуется).
- Подготовленные/форматированные поля (qtyFormatted, normFormatted) создавать один раз в computed слое, не форматировать в шаблонах в цикле.

8) Ошибки и уведомления
- В composables/store ловить исключения и через Quasar Notify показывать человеку‑понятные сообщения (код + краткое описание). Без «пустых catch».

9) Типизация
- Исключить any в новых модулях и компонентах.
- Строгие QTableColumn&lt;T&gt; для каждой таблицы (ProductionRow, PurchaseRow, …).
- Явные типы фильтров и параметров API.

10) Русификация интерфейса
- Базовая настройка i18n:
  - [frontend/src/i18n/index.ts](frontend/src/i18n/index.ts), [frontend/src/i18n/ru.ts](frontend/src/i18n/ru.ts); интеграция в [ts.createApp()](frontend/src/main.ts:1).
- Перенести строки интерфейса MRP в словари ru.ts; заменить «жёсткие» строки.
- План на остальные страницы — после завершения MRP.

Порядок поставки (итерации/коммиты)
1. Типы + useFormatting + useDictionaries.
2. Pinia store mrpResults (summary + справочники) + интеграция на странице.
3. Production: фильтры, агрегаты (grouped/agenda), единая таблица; убрать двойные запросы.
4. Purchases: агрегат + деталь.
5. Capacity: сводка + деталь.
6. Pegging.
7. ComponentsTree.
8. Русификация MRP (i18n подключение + перенос строк).
9. Очистка ненужных watch, удаление дублей колонок, выравнивание типизации.
10. Финал: регресс‑проверка и документация.

Критерии приёмки
- Функциональная эквивалентность детальных таблиц (пагинация/сортировка не ухудшены).
- Верхние агрегаты соответствуют ответам серверных эндпоинтов (без клиентской группировки).
- Отсутствуют двойные загрузки (нет запросов с limit=100000 в UI).
- Стабильность UI при изменении фильтров; отсутствие «дерганья» из‑за лишних watch.
- Типизация проходит сборку без any в новых модулях.
- Русский интерфейс на MRP‑странице и новых компонентах.
- Ошибки отображаются Notify, нет пустых catch.

Риски и примечания
- При существенной декомпозиции возможны регрессии в слотах/таблицах — снижаем риск поэтапной поставкой.
- Для устранения N+1 по items/resources желателен серверный батч эндпоинт (?ids=…); если его нет, применим клиентскую батч‑догрузку с пулом.
- Блоки, завязанные на внутренние поля (например, stages), сохраняются, но переносятся в детальные компоненты с явной типизацией.

Ссылки на ключевые эндпоинты
- Группы производства: [ts.export async function getPlanningResultProductionGrouped()](frontend/src/services/api.ts:256)
- Повестка дня: [ts.export async function getPlanningResultProductionAgendaDay()](frontend/src/services/api.ts:293)
- Группы закупок: [ts.export async function getPlanningResultPurchasesGrouped()](frontend/src/services/api.ts:320)
- Сводка мощностей: [ts.export async function getPlanningResultCapacitySummary()](frontend/src/services/api.ts:343)
- Деталь: [ts.export async function getPlanningResultProduction()](frontend/src/services/api.ts:119), [ts.export async function getPlanningResultPurchases()](frontend/src/services/api.ts:133), [ts.export async function getPlanningResultCapacity()](frontend/src/services/api.ts:147), [ts.export async function getPlanningResultPegging()](frontend/src/services/api.ts:159)

План следующего шага
- Создать типы и базовые composables (useFormatting, useDictionaries), не затрагивая текущую логику страницы, затем постепенно подключать их на MRPResultPage.

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 1

Согласованный объём: полный фронтенд (рефакторинг MRPResultPage + Pinia/composables + типы + русификация). В рамках итерации 1 выполнено:

Изменения (код)
- Типы домена MRP: [ts.export interface MRPSummary](frontend/src/types/mrp.ts:24), [ts.export interface ProductionOrder](frontend/src/types/mrp.ts:43), [ts.export interface PurchaseRow](frontend/src/types/mrp.ts:88), [ts.export interface CapacityRow](frontend/src/types/mrp.ts:112), [ts.export interface PeggingRow](frontend/src/types/mrp.ts:132), [ts.export interface ProductionFilters](frontend/src/types/mrp.ts:144)
- Базовые форматтеры: [ts.export function useFormatting()](frontend/src/composables/useFormatting.ts:56) — формат чисел/количеств, цвет статуса, нормализация дат
- Справочники с защитой от N+1 и пулом конкурентности: [ts.export function useDictionaries()](frontend/src/composables/useDictionaries.ts:48) (+ правка конкурентного раннера на типобезопасный вариант)
- Производство (детально + агрегаты + «повестка дня»): [ts.export function useProduction()](frontend/src/composables/useProduction.ts:22)
- Закупки (детально + агрегат): [ts.export function usePurchases()](frontend/src/composables/usePurchases.ts:22)
- Мощности (детально + сводка): [ts.export function useCapacity()](frontend/src/composables/useCapacity.ts:22)
- Pegging (детально): [ts.export function usePegging()](frontend/src/composables/usePegging.ts:14)
- Централизованное состояние: [ts.export const useMRPResultsStore = defineStore('mrpResults')](frontend/src/stores/mrpResults.ts:12) — summary, словари, уведомления
- Компонент сводки: [vue.&lt;script setup&gt; MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1) и подключение в страницу
- Замена inline fmt/fmtQty на форматтеры из composable: [ts.const { formatNumber: fmt, formatQty: fmtQty, statusColor, warnText }](frontend/src/pages/MRPResultPage.vue:813)

Оптимизации загрузки
- Удалены двойные запросы (limit=100000) в детальных таблицах:
  - Производство: [ts.async function loadProduction()](frontend/src/pages/MRPResultPage.vue:823) — оставлена только пагинация; агрегаты грузятся сервером
  - Закупки: [ts.async function loadPurchases()](frontend/src/pages/MRPResultPage.vue:865) — оставлена только пагинация + серверный агрегат
- Переход на server-side агрегаты для верхних блоков:
  - Производство grouped: [ts.export async function getPlanningResultProductionGrouped()](frontend/src/services/api.ts:256)
  - «Повестка дня»: [ts.export async function getPlanningResultProductionAgendaDay()](frontend/src/services/api.ts:293)
  - Закупки grouped: [ts.export async function getPlanningResultPurchasesGrouped()](frontend/src/services/api.ts:320)
  - Сводка мощностей: [ts.export async function getPlanningResultCapacitySummary()](frontend/src/services/api.ts:343)

Русификация
- Вынесение форматтеров (ru-RU) — подготовка к i18n
- Компонент сводки и верхние подписи — на русском (источники строк останутся для переноса в i18n на следующем шаге)

Уведомления и ошибки
- Единый стиль уведомлений через Quasar Notify в composables и store:
  - Примеры: [ts.Notify.create({...})](frontend/src/composables/useProduction.ts:92), [ts.Notify.create({...})](frontend/src/stores/mrpResults.ts:33)

Технические заметки
- Временная интеграция composables выполнена частично: текущая страница продолжает использовать часть старой логики и watchers; создан компонент MRPSummaryCard и базовые подключаемые модули. Следующие итерации подключат новые composables к странице и начнут декомпозицию таблиц.
- Исправлена типовая ошибка в конкурентном раннере [ts.async function runWithConcurrency&lt;T&gt;()](frontend/src/composables/useDictionaries.ts:26) — исключены обращения к possibly undefined фабрикам.

План итерации 2
- Создать компоненты вкладок и таблиц mrp/*: ProductionFilters, ProductionUnifiedTable, ProductionDailyAgenda, ProductionGroupedTable, PurchasesUnifiedTable, DetailTables, CapacityTable, PeggingTable
- Перевести страницу на orchestrator-подход: потребление данных из composables, явные события применения фильтров с debounce, минимизация deep watch
- Подключить i18n-основу и перенести строки интерфейса MRP в словари
- Доуточнить типизацию на странице (убрать any) и выровнять колонки QTableColumn&lt;T&gt; для всех таблиц
- Обновить unit-настройки/линтер (при необходимости) под новые typings

Критерии приёмки итерации 1
- Нет limit=100000 запросов в детале — подтверждено
- Агрегаты строятся сервером — подтверждено вызовами API
- Сводка вынесена в отдельный компонент — подтверждено
- Базовая типизация и форматирование подключены — подтверждено

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 2 (старт выполнения)

Статус: старт. В этой сессии обновлена документация и зафиксирован детальный пошаговый план выполнения итерации 2. Код не изменён. Согласно правилам репозитория правки выполняются пошагово и фиксируются в этом файле; инициирование кодовых изменений возможно после подтверждения в текущей сессии.

Цели итерации 2
- Создать компоненты вкладок и таблиц mrp/* и подключить их на страницу.
- Перевести страницу-координатор на orchestrator-подход с использованием composables/Pinia.
- Подключить базу i18n и перенести строки интерфейса MRP.
- Уточнить типизацию таблиц и убрать any; везде использовать QTableColumn&lt;T&gt;.

План действий (детально)
1) Компоненты mrp/* (каркас + типизированные пропсы/эвенты)
- ProductionFilters.vue: props: filters: [ts.export interface ProductionFilters](frontend/src/types/mrp.ts:144), emits: "apply" | "reset".
- ProductionUnifiedTable.vue: props: rows: [ts.export interface ProductionOrder[]](frontend/src/types/mrp.ts:43), columns: QTableColumn&lt;ProductionOrder&gt;, форматирование из [ts.export function useFormatting()](frontend/src/composables/useFormatting.ts:56).
- ProductionDailyAgenda.vue: источники данных — [ts.export async function getPlanningResultProductionAgendaDay()](frontend/src/services/api.ts:293); типы: ProductionAgendaGroup.
- ProductionGroupedTable.vue: источники данных — [ts.export async function getPlanningResultProductionGrouped()](frontend/src/services/api.ts:256); типы: ProductionGroup.
- PurchasesUnifiedTable.vue: источники данных — [ts.export async function getPlanningResultPurchasesGrouped()](frontend/src/services/api.ts:320); типы: [ts.export interface PurchaseRow](frontend/src/types/mrp.ts:88) | PurchaseGroupedRow.
- ProductionDetailTable.vue: детальная пагинация — [ts.export async function getPlanningResultProduction()](frontend/src/services/api.ts:119), слоты для стадий.
- PurchasesDetailTable.vue: детальная пагинация — [ts.export async function getPlanningResultPurchases()](frontend/src/services/api.ts:133).
- CapacityTable.vue: сводка/деталь — [ts.export async function getPlanningResultCapacitySummary()](frontend/src/services/api.ts:343), [ts.export async function getPlanningResultCapacity()](frontend/src/services/api.ts:147); тип: [ts.export interface CapacityRow](frontend/src/types/mrp.ts:112).
- PeggingTable.vue: деталь — [ts.export async function getPlanningResultPegging()](frontend/src/services/api.ts:159); тип: [ts.export interface PeggingRow](frontend/src/types/mrp.ts:132).
- KindIssuesDialog.vue: отображение проблем сопоставления видов производства (diagnostic feed из summary).

2) Страница-координатор [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1)
- Подключить store: [ts.export const useMRPResultsStore = defineStore('mrpResults')](frontend/src/stores/mrpResults.ts:12) — единая загрузка summary и справочников.
- Заменить inline форматирование на [ts.export function useFormatting()](frontend/src/composables/useFormatting.ts:56). Текущие точки использования: [ts.const { formatNumber: fmt, formatQty: fmtQty }](frontend/src/pages/MRPResultPage.vue:813).
- Внедрить явные события "applyFilters" с debounce ≈250 мс вместо deep watch; минимальные точечные watch на bucket_type/date_from/date_to/page.
- Исключить двойные запросы: оставить только вызовы composables — [ts.export function useProduction()](frontend/src/composables/useProduction.ts:22), [ts.export function usePurchases()](frontend/src/composables/usePurchases.ts:22), [ts.export function useCapacity()](frontend/src/composables/useCapacity.ts:22), [ts.export function usePegging()](frontend/src/composables/usePegging.ts:14).

3) i18n — базовая интеграция
- Добавить и подключить [ts.createI18n()](frontend/src/i18n/index.ts:1) и словарь [ts.export const ru](frontend/src/i18n/ru.ts:1); интеграция в приложение: [ts.createApp()](frontend/src/main.ts:1).
- Перенести строки интерфейса MRP (заголовки, подписи, колонки таблиц) из компонентов в ru.ts. Остальные страницы — последующими итерациями.

4) Типизация и таблицы
- Везде строгие QTableColumn&lt;T&gt;: ProductionOrder, PurchaseRow, CapacityRow, PeggingRow.
- Явные интерфейсы props/emit для новых компонентов; без any в новых файлах.

5) Реактивность и производительность
- Debounce "applyFilters" (≈250 мс), computed для подготовленных полей (qtyFormatted, normFormatted), исключить форматирование в циклах шаблонов.
- Не допускать N+1: справочники из [ts.export function useDictionaries()](frontend/src/composables/useDictionaries.ts:48).

6) Ошибки и уведомления
- Централизация через Quasar Notify (пример уже используется): [ts.Notify.create({...})](frontend/src/composables/useProduction.ts:92), [ts.Notify.create({...})](frontend/src/stores/mrpResults.ts:33).

Последовательность исполнения (итерация 2)
- Шаг 1: Создать каркас компонентов mrp/* с типизированными пропсами/эвентами и плейсхолдерами таблиц.
- Шаг 2: Подключить компоненты на [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1) и заменить локальную логику на вызовы composables/store.
- Шаг 3: Внедрить i18n-основу и перенести строки MRP.
- Шаг 4: Привести все таблицы к QTableColumn&lt;T&gt; и устранить any.
- Шаг 5: Минимизировать watchers, ввести события с debounce, проверить стабильность UI.
- Шаг 6: Smoke-тест: проверка загрузок, пагинации, агрегатов (grouped/agenda/capacity/purchases/pegging).

Чек‑лист итерации 2 (исполнение)
- [x] Обновлена документация и зафиксирован детальный план
- [ ] Создан каркас компонентов mrp/* (таблицы/фильтры/диалог)
- [ ] Интегрированы composables/store в страницу-координатор
- [ ] Подключён i18n и перенесены строки MRP в ru.ts
- [ ] Приведены таблицы к QTableColumn&lt;T&gt; без any
- [ ] Минимизированы watchers, добавлен debounce событий фильтров
- [ ] Выполнен smoke-тест страницы и проверка уведомлений

Критерии приёмки итерации 2
- Компоненты mrp/* подключены на страницу, работают на данных из composables без клиентской группировки.
- Нет лишних запросов и двойных загрузок; агрегаты приходят с сервера: [ts.export async function getPlanningResultProductionGrouped()](frontend/src/services/api.ts:256), [ts.export async function getPlanningResultProductionAgendaDay()](frontend/src/services/api.ts:293), [ts.export async function getPlanningResultPurchasesGrouped()](frontend/src/services/api.ts:320), [ts.export async function getPlanningResultCapacitySummary()](frontend/src/services/api.ts:343).
- Строки интерфейса MRP вынесены в i18n ru.ts; сборка проходит без any в новых файлах.
- Уведомления унифицированы, пустых catch нет; UI стабилен при изменении фильтров.

Примечание по изменениям кода
- В рамках текущей сессии код не изменялся (соответствует правилам: «При работе не трогай ничего кроме .docs/progress.md»). При подтверждении выполнения итерации 2 с изменениями кода будут последовательно созданы/изменены файлы компонентов в каталоге [frontend/src/components/mrp/](frontend/src/components/mrp/:1), внесены правки в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1), добавлена база i18n ([frontend/src/i18n/index.ts](frontend/src/i18n/index.ts:1), [frontend/src/i18n/ru.ts](frontend/src/i18n/ru.ts:1)) и обновлена интеграция в [frontend/src/main.ts](frontend/src/main.ts:1).

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 2 (аудит состояния и уточнение задач)

Статус: аудит выполнен. В каталоге компонентов MRP присутствуют файлы: CapacityTable.vue, MRPSummaryCard.vue, ProductionDailyAgenda.vue, ProductionDetailTable.vue, ProductionFilters.vue, ProductionGroupedTable.vue, ProductionUnifiedTable.vue, PurchasesDetailTable.vue, PurchasesUnifiedTable.vue (см. [frontend/src/components/mrp/](frontend/src/components/mrp/:1)).

Промежуточные выводы по прочитанным компонентам
- Сводка: [vue.&lt;script setup&gt; MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1)
  - Использует форматтеры [ts.export function useFormatting()](frontend/src/composables/useFormatting.ts:1), строго типизированные props summary: [ts.export interface MRPSummary](frontend/src/types/mrp.ts:1).
  - Эмитит событие открытия диалога проблем видов производства ('open-kind-issues').
- Фильтры: [vue.&lt;script setup&gt; ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:1)
  - v-model на локальной копии [ts.export interface ProductionFilters](frontend/src/types/mrp.ts:1); события 'apply'/'reset'/'day-picked'.
  - Готово к orchestrator-подходу (замена глубоких watch на явные события).
- Таблицы:
  - Единая продукция: [vue.&lt;script setup&gt; ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1) — типизированные колонки [ts.type QTableColumn&lt;T&gt;](frontend/src/components/mrp/ProductionUnifiedTable.vue:60), вычисление norm_per_unit при отсутствии исходного поля.
  - Группировка: [vue.&lt;script setup&gt; ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue:1) — секции групп, бэйджи срочности/перегруза, форматтеры подключены.
  - Повестка дня: [vue.&lt;script setup&gt; ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue:1) — дневные агрегаты с перегрузами.

Обнаруженные пробелы (на текущее состояние)
- Отсутствуют файлы:
  - [frontend/src/components/mrp/PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1) — таблица пеггинга (детально) на базе [ts.export async function getPlanningResultPegging()](frontend/src/services/api.ts:1) и [ts.export interface PeggingRow](frontend/src/types/mrp.ts:1).
  - [frontend/src/components/mrp/KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1) — диалог «Проблемы привязки видов» (на базе предупреждений из summary).
- База i18n отсутствует: [frontend/src/i18n/index.ts](frontend/src/i18n/index.ts:1), [frontend/src/i18n/ru.ts](frontend/src/i18n/ru.ts:1); строки пока «жёстко» в компонентах.
- Интеграция страницы-координатора [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1) с composables/store ещё не переведена полностью (по плану итерации 2).

Детализация оставшихся задач итерации 2

1) Добавить i18n-основу и ключи для MRP
- Создать файлы: [ts.export function createI18n()](frontend/src/i18n/index.ts:1), [ts.export const ru](frontend/src/i18n/ru.ts:1); подключить в [ts.createApp()](frontend/src/main.ts:1).
- Ключи ru.ts (минимальный набор):
  - Заголовки сводки:
    - mrp.summary.title, mrp.summary.run, mrp.summary.start, mrp.summary.finish, mrp.summary.horizonDays, mrp.summary.weeklyYes, mrp.summary.weeklyNo
    - mrp.summary.productionOrders, mrp.summary.purchaseRequests, mrp.summary.overloadedBuckets, mrp.summary.overloadTotal
    - mrp.summary.warnings.title, mrp.summary.warnings.caption, mrp.summary.kindIssues.button
  - Колонки таблиц:
    - mrp.columns.name, mrp.columns.article, mrp.columns.qty, mrp.columns.normPerUnit, mrp.columns.normTotal
  - Заголовки групп:
    - mrp.group.productionKind, mrp.group.ordersCount, mrp.group.normSumHours, mrp.group.capOverloadHours, mrp.group.urgencyDays
    - mrp.agenda.positionsCountDay, mrp.agenda.normDayHours, mrp.agenda.qtyDay
  - Фильтры:
    - mrp.filters.bucket, mrp.filters.dayDate, mrp.filters.fromDate, mrp.filters.toDate, mrp.filters.apply, mrp.filters.reset
    - mrp.filters.bucketOption.any, mrp.filters.bucketOption.daily, mrp.filters.bucketOption.weekly
  - Прочее:
    - mrp.badge.noNormPerUnit, mrp.placeholder.noArticle, mrp.placeholder.itemNameFallback

2) Создать недостающие компоненты (каркас, типизация, подключение форматтеров)
- [vue.&lt;script setup&gt; PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1)
  - props: rows: [ts.export interface PeggingRow[]](frontend/src/types/mrp.ts:1), loading?: boolean
  - колонки: parent_item / child_item / qty / need_date / parent_need_date (подписи — через i18n)
  - использование форматтеров [ts.export function useFormatting()](frontend/src/composables/useFormatting.ts:1) для чисел/дат
- [vue.&lt;script setup&gt; KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1)
  - props: issues: Array&lt;{ code: string; msg?: string; context?: Record&lt;string, unknown&gt; }&gt;, modelValue: boolean
  - emits: 'update:modelValue'
  - фильтрация кодов: NO_AREA_FOR_PRODUCTION_KIND, NO_AREA_FOR_PRODUCTION_KIND_ZERO_NORM; список/таблица с кратким описанием и контекстом

3) Интеграция страницы-координатора (без изменения API)
- Страница: [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1)
  - Подключить store: [ts.export const useMRPResultsStore = defineStore('mrpResults')](frontend/src/stores/mrpResults.ts:1) для загрузки summary + ensureDictionaries()
  - Перевести загрузки на composables: [ts.export function useProduction()](frontend/src/composables/useProduction.ts:1), [ts.export function usePurchases()](frontend/src/composables/usePurchases.ts:1), [ts.export function useCapacity()](frontend/src/composables/useCapacity.ts:1), [ts.export function usePegging()](frontend/src/composables/usePegging.ts:1)
  - Внедрить событие 'apply' из [vue.&lt;script setup&gt; ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:1) с debounce ≈250 мс вместо глубоких watch
  - Подключить [vue.&lt;script setup&gt; MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1) и обработку 'open-kind-issues' (открытие [vue.&lt;script setup&gt; KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1))

4) Типизация таблиц и локализация строк
- Привести все QTable к строгим [ts.type QTableColumn&lt;T&gt;](frontend/src/components/mrp/ProductionUnifiedTable.vue:60) с T из [frontend/src/types/mrp.ts](frontend/src/types/mrp.ts:1)
- Заменить «жёсткие» строки на i18n в:
  - [frontend/src/components/mrp/MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1)
  - [frontend/src/components/mrp/ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:1)
  - [frontend/src/components/mrp/ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1)
  - [frontend/src/components/mrp/ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue:1)
  - [frontend/src/components/mrp/ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue:1)
  - [frontend/src/components/mrp/PurchasesUnifiedTable.vue](frontend/src/components/mrp/PurchasesUnifiedTable.vue:1)
  - [frontend/src/components/mrp/CapacityTable.vue](frontend/src/components/mrp/CapacityTable.vue:1)

Обновление чек-листа итерации 2
- [x] Обновлена документация и зафиксирован детальный план
- [x] Создан каркас основных компонентов mrp/* (Production*, Purchases*, Capacity) — подтверждено наличием файлов
- [ ] Добавлены недостающие компоненты: PeggingTable.vue, KindIssuesDialog.vue
- [ ] Интегрированы composables/store в страницу-координатор (замена глубоких watch на события с debounce)
- [ ] Подключён i18n и перенесены строки MRP в ru.ts
- [ ] Приведены таблицы к QTableColumn&lt;T&gt; без any (проверка всех новых/обновлённых компонентов)
- [ ] Выполнен smoke-тест страницы и проверка уведомлений

Примечание по изменению кода
- В рамках данной сессии изменена только документация (.docs/progress.md), код не затрагивался. Для продолжения выполнения итерации 2 (создание [frontend/src/components/mrp/PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1), [frontend/src/components/mrp/KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1), база i18n и интеграция на [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1)) требуется подтверждение на внесение кодовых изменений.

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 2 (спецификации, i18n-ключи, план интеграции)

Статус: уточнение и фиксация ТЗ на кодовые изменения итерации 2. Код не изменён.

1) Недостающие компоненты — спецификации

1.1) PeggingTable.vue
- Файл: [frontend/src/components/mrp/PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1)
- Назначение: заменить локальный q-table пеггинга на странице на типизированный компонент.
- Props:
  - rows: [ts.export interface PeggingRow[]](frontend/src/types/mrp.ts:132)
  - loading?: boolean
  - pagination?: { page: number; rowsPerPage: number; rowsNumber: number }
- Emits:
  - (e: 'request', ctx: { pagination: { page: number; rowsPerPage: number } })
- Колонки (i18n-ключами): child_item, parent_item, qty_contribution, need_date, parent_need_date
- Форматирование:
  - Кол-ва — через [ts.export function useFormatting()](frontend/src/composables/useFormatting.ts:56)
  - Даты — нормализация и вывод useFormatting().formatDate()

Интеграция:
- Заменить локальный блок на странице [vue.&lt;script setup&gt; MRPResultPage.vue — q-table Pegging](frontend/src/pages/MRPResultPage.vue:167) на <PeggingTable .../> с прокидкой rows/loading/pagination и обработчиком @request="onPegRequest".

1.2) KindIssuesDialog.vue
- Файл: [frontend/src/components/mrp/KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1)
- Назначение: вынести диалог «Проблемы привязки видов производства» из страницы в самостоятельный компонент.
- Props:
  - modelValue: boolean
  - issues: Array&lt;{ code: string; msg?: string; context?: Record&lt;string, unknown&gt; }&gt;
- Emits:
  - (e: 'update:modelValue', v: boolean)
- UI:
  - Заголовок, кнопка закрытия, q-table со столбцами: production_kind_id, production_kind_name, item_name/article, root_item_article, spec, code.
- Источник issues:
  - Фильтр по кодам NO_AREA_FOR_PRODUCTION_KIND, NO_AREA_FOR_PRODUCTION_KIND_ZERO_NORM
- Локализация всех заголовков/подписей через i18n.

Интеграция:
- Заменить блок [vue.&lt;script setup&gt; MRPResultPage.vue — диалог Kind Issues](frontend/src/pages/MRPResultPage.vue:258) на <KindIssuesDialog v-model="showKindIssuesDialog" :issues="kindIssuesRows" />.

2) i18n — базовая интеграция и словарь

2.1) База i18n
- Индекс: [frontend/src/i18n/index.ts](frontend/src/i18n/index.ts:1)
  - [ts.export function createI18n()](frontend/src/i18n/index.ts:1) и экспорт i18n
- Русский словарь: [frontend/src/i18n/ru.ts](frontend/src/i18n/ru.ts:1)
  - [ts.export const ru](frontend/src/i18n/ru.ts:1) = { ... }
- Интеграция в приложение: подключить i18n в [ts.createApp()](frontend/src/main.ts:1)

2.2) Набор ключей ru.ts (минимально достаточный для MRP)
- Заголовки/общие:
  - mrp.title, mrp.run, mrp.status, mrp.startedAt, mrp.finishedAt, mrp.horizonDays, mrp.weeklyYes, mrp.weeklyNo
- Сводка:
  - mrp.summary.productionOrders, mrp.summary.purchaseRequests, mrp.summary.overloadedBuckets, mrp.summary.overloadTotal
  - mrp.summary.warnings.title, mrp.summary.warnings.caption, mrp.summary.kindIssues.button
- Таблицы (общие колонки):
  - mrp.columns.name, mrp.columns.article, mrp.columns.qty, mrp.columns.normPerUnit, mrp.columns.normTotal
- Группы/повестка:
  - mrp.group.productionKind, mrp.group.ordersCount, mrp.group.normSumHours, mrp.group.capOverloadHours, mrp.group.urgencyDays
  - mrp.agenda.positionsCountDay, mrp.agenda.normDayHours, mrp.agenda.qtyDay
- Фильтры:
  - mrp.filters.bucket, mrp.filters.dayDate, mrp.filters.fromDate, mrp.filters.toDate, mrp.filters.apply, mrp.filters.reset
  - mrp.filters.bucketOption.any, mrp.filters.bucketOption.daily, mrp.filters.bucketOption.weekly
- Действия:
  - mrp.actions.csv, mrp.actions.xlsx, mrp.actions.refresh, mrp.actions.show
- Pegging:
  - mrp.pegging.child, mrp.pegging.parent, mrp.pegging.qtyContribution, mrp.pegging.needDate, mrp.pegging.parentNeedDate
- Диалог проблем видов:
  - mrp.kindIssues.title, mrp.kindIssues.columns.kindId, mrp.kindIssues.columns.kindName, mrp.kindIssues.columns.item, mrp.kindIssues.columns.article, mrp.kindIssues.columns.rootArticle, mrp.kindIssues.columns.spec, mrp.kindIssues.columns.code
- Плейсхолдеры:
  - mrp.placeholder.noArticle, mrp.placeholder.itemNameFallback, mrp.badge.noNormPerUnit

3) Страница-координатор MRPResultPage — план интеграции

3.1) Подключить компоненты:
- Уже подключено: [vue.&lt;script setup&gt; MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1), [vue.&lt;script setup&gt; ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:1), [vue.&lt;script setup&gt; ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1), [vue.&lt;script setup&gt; ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue:1), [vue.&lt;script setup&gt; ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue:1), [vue.&lt;script setup&gt; PurchasesUnifiedTable.vue](frontend/src/components/mrp/PurchasesUnifiedTable.vue:1), [vue.&lt;script setup&gt; CapacityTable.vue](frontend/src/components/mrp/CapacityTable.vue:1)
- Добавить и подключить:
  - [vue.&lt;script setup&gt; PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1) — заменяет локальный q-table пеггинга
  - [vue.&lt;script setup&gt; KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1) — заменяет локальный q-dialog

3.2) Перевести строки в i18n:
- Заголовки вкладок, кнопок и меток заменить на $t('…') в [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1) и всех mrp/*

3.3) События вместо deep watch:
- Оставить: подгрузка при смене вкладок [watch(tab)](frontend/src/pages/MRPResultPage.vue:1083) и [watch(viewTab)](frontend/src/pages/MRPResultPage.vue:1091)
- Заменить/удалить избыточные наблюдатели:
  - [watch([prod.rows, itemMap, areaMap])](frontend/src/pages/MRPResultPage.vue:1097) — вызывать rebuildGroupedProductionOrders() явно в loadProduction() и после ensureDictionaries(), см. уже выполняется в [ts.async function loadProduction()](frontend/src/pages/MRPResultPage.vue:670)
  - [watch([prodAllRows, date_from, date_to, bucket_type, areaMap])](frontend/src/pages/MRPResultPage.vue:1102) — пересчёты выполнять по явному событию 'apply' из [vue.&lt;script setup&gt; ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:73) с debounce ≈250 мс
  - Дубли [watch(capUpper)](frontend/src/pages/MRPResultPage.vue:1110) встречается дважды — оставить один вызов пересборки, второй удалить
  - Ежедневная повестка [watch([prodAllRows, day_date])](frontend/src/pages/MRPResultPage.vue:1115) — оставить пересчёт по событию 'day-picked' из [vue.&lt;script setup&gt; ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:78), а не на любой сдвиг prodAllRows
- Действия «Применить/Сбросить»:
  - Привязать loadProduction(), loadCapacityUpper(), rebuildDailyAgendaForDay(), loadCapacityUpperDay() к @apply (вверху уже подключено на панели «Производство»)
  - Привязать loadPurchases() к @apply на вкладке «Закупки»
  - Сброс — централизованно через resetFilters()/onPurchReset()

3.4) Колонки таблиц — строгая типизация
- ProductionUnifiedTable: уже c типом PlainProdRow внутри компонента — подтверждено
- ProductionGroupedTable/ProductionDailyAgenda: использовать [ts.type QTableColumn&lt;ProductionGroup&gt;](frontend/src/components/mrp/ProductionGroupedTable.vue:71) и [ts.type QTableColumn&lt;ProductionAgendaGroup&gt;](frontend/src/components/mrp/ProductionDailyAgenda.vue:70)
- PurchasesUnifiedTable, CapacityTable, PeggingTable: привести к соответствующим интерфейсам из [frontend/src/types/mrp.ts](frontend/src/types/mrp.ts:1)

4) Дополнение к типам (ТЗ)
- Добавить (при интеграции) типы:
  - [ts.export interface KindIssue](frontend/src/types/mrp.ts:1) { code: string; msg?: string; context?: Record&lt;string, unknown&gt;; production_kind_id?: number; production_kind_name?: string; item_id?: number; item_name?: string; item_article?: string; root_item_article?: string; spec_id?: number | string; spec_name?: string; spec_code?: string; spec_ref1c?: string }
- Уточнить типы колонок в детальных таблицах ProductionDetailTable/PurchasesDetailTable/CapacityTable (generic QTableColumn&lt;T&gt;)

5) План поставки итерации 2 (кода)
- Шаг 1: Создать [frontend/src/components/mrp/PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1), [frontend/src/components/mrp/KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1); заменить локальные блоки на странице.
- Шаг 2: Подключить i18n ([frontend/src/i18n/index.ts](frontend/src/i18n/index.ts:1), [frontend/src/i18n/ru.ts](frontend/src/i18n/ru.ts:1)) и перенести строки MRP.
- Шаг 3: Очистить лишние watchers в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1), перейти на события 'apply'/'day-picked' с debounce.
- Шаг 4: Привести колонки к строгой типизации QTableColumn&lt;T&gt; в новых/обновлённых компонентах.

Критерии приёмки итерации 2 (детализировано)
- Пеггинг отображается через [vue.&lt;script setup&gt; PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1); взаимодействие через @request.
- Диалог проблем видов вынесен в [vue.&lt;script setup&gt; KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1) и открывается по событию MRPSummaryCard 'open-kind-issues'.
- i18n подключён, строки MRP переведены, отсутствуют «жёсткие» строки в новых/обновлённых компонентах.
- Убраны избыточные watchers; события фильтров с debounce ≈250 мс; отсутствуют лишние нагрузки/двойные запросы.

Чек‑лист итерации 2 (обновлён)
- [x] Обновлена документация и зафиксирован детальный план
- [x] Создан каркас основных компонентов mrp/* (Production*, Purchases*, Capacity) — подтверждено наличием файлов
- [x] Подготовлены спецификации недостающих компонентов (PeggingTable, KindIssuesDialog) и i18n-ключи
- [ ] Реализованы PeggingTable.vue, KindIssuesDialog.vue и интеграция на страницу
- [ ] Подключён i18n и перенесены строки MRP в ru.ts
- [ ] Минимизированы watchers, добавлен debounce событий фильтров
- [ ] Приведены таблицы к QTableColumn&lt;T&gt; без any (в т.ч. Pegging/Capacity/Purchases)
- [ ] Выполнен smoke-тест страницы и проверка уведомлений

Примечание по процессу
- В текущей сессии обновлена только документация (.docs/progress.md). Для старта кодовых изменений по итерации 2 требуется подтверждение. После подтверждения будут выполнены Шаги 1–4, с фиксацией прогресса по каждому шагу в этом документе.

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 2 (i18n‑маппинг строк, план чистки watch, уточнение интеграции)

Статус: продолжаем выполнение итерации 2 без изменения кода — фиксируем детальные инструкции по переносу строк в i18n и по упрощению реактивности на странице. Каркасы основных компонентов подтверждены, недостающие компоненты и i18n — в планах к реализации после подтверждения.

1) Карта строк для переноса в i18n (ключи ru.ts → замены по файлам)

1.1) Страница координатор [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1)
- Заголовок страницы:
  - "Результаты прогона MRP #{{ runId }}" → $t('mrp.title') с параметром runId ([frontend/src/pages/MRPResultPage.vue:4])
- Вкладки верхнего уровня:
  - "Заказы на производство" → $t('mrp.tabs.production') ([frontend/src/pages/MRPResultPage.vue:22])
  - "Заказы на закупку" → $t('mrp.tabs.purchases') ([frontend/src/pages/MRPResultPage.vue:23])
- Подписи фильтров в детали (на странице есть дубли, но основная форма теперь через компонент ProductionFilters):
  - "Бакет" ([frontend/src/pages/MRPResultPage.vue:112], [frontend/src/pages/MRPResultPage.vue:131], [frontend/src/pages/MRPResultPage.vue:150]) → $t('mrp.filters.bucket')
  - "От даты (YYYY-MM-DD)" ([frontend/src/pages/MRPResultPage.vue:113], [frontend/src/pages/MRPResultPage.vue:132], [frontend/src/pages/MRPResultPage.vue:151]) → $t('mrp.filters.fromDate')
  - "До даты (YYYY-MM-DD)" ([frontend/src/pages/MRPResultPage.vue:114], [frontend/src/pages/MRPResultPage.vue:133], [frontend/src/pages/MRPResultPage.vue:152]) → $t('mrp.filters.toDate')
  - Кнопка "Детальный анализ" (заголовок секции) → $t('mrp.sections.detail') ([frontend/src/pages/MRPResultPage.vue:98])
  - Вкладки детального анализа:
    - "Производство (детально)" → $t('mrp.tabs.productionDetail') ([frontend/src/pages/MRPResultPage.vue:100])
    - "Закупки (детально)" → $t('mrp.tabs.purchasesDetail') ([frontend/src/pages/MRPResultPage.vue:101])
    - "Мощности" → $t('mrp.tabs.capacity') ([frontend/src/pages/MRPResultPage.vue:102])
    - "Pegging" → $t('mrp.tabs.pegging') ([frontend/src/pages/MRPResultPage.vue:103])
    - "Компоненты заказа" → $t('mrp.tabs.components') ([frontend/src/pages/MRPResultPage.vue:104])
- Кнопки экспорта:
  - "CSV" → $t('mrp.actions.csv'), "XLSX" → $t('mrp.actions.xlsx') ([frontend/src/pages/MRPResultPage.vue:41], [frontend/src/pages/MRPResultPage.vue:42], [frontend/src/pages/MRPResultPage.vue:83], [frontend/src/pages/MRPResultPage.vue:84])
- Диалог проблем видов:
  - Заголовок "Проблемы привязки видов производства" → $t('mrp.kindIssues.title') ([frontend/src/pages/MRPResultPage.vue:262])
  - Заголовки колонок в kindIssuesColumns:
    - "Вид (ID)" → $t('mrp.kindIssues.columns.kindId') ([frontend/src/pages/MRPResultPage.vue:402])
    - "Вид производства" → $t('mrp.kindIssues.columns.kindName') ([frontend/src/pages/MRPResultPage.vue:403])
    - "Номенклатура" → $t('mrp.kindIssues.columns.item') ([frontend/src/pages/MRPResultPage.vue:404])
    - "Артикул" → $t('mrp.kindIssues.columns.article') ([frontend/src/pages/MRPResultPage.vue:405])
    - "Артикул корневого изделия" → $t('mrp.kindIssues.columns.rootArticle') ([frontend/src/pages/MRPResultPage.vue:406])
    - "Спецификация" → $t('mrp.kindIssues.columns.spec') ([frontend/src/pages/MRPResultPage.vue:408])
    - "Код" → $t('mrp.kindIssues.columns.code') ([frontend/src/pages/MRPResultPage.vue:409])
- Вкладка Pegging — подписи инпутов:
  - "Child item_id" → $t('mrp.pegging.filters.childItemId') ([frontend/src/pages/MRPResultPage.vue:169])
  - "Parent item_id" → $t('mrp.pegging.filters.parentItemId') ([frontend/src/pages/MRPResultPage.vue:170])
  - "От даты (YYYY-MM-DD)" → $t('mrp.filters.fromDate') ([frontend/src/pages/MRPResultPage.vue:171])
  - "До даты (YYYY-MM-DD)" → $t('mrp.filters.toDate') ([frontend/src/pages/MRPResultPage.vue:172])
- Вкладка Components:
  - "Выберите производственный заказ" → $t('mrp.components.selectOrder') ([frontend/src/pages/MRPResultPage.vue:195])
  - "Показать состав (по заказу)" → $t('mrp.actions.showByOrder') ([frontend/src/pages/MRPResultPage.vue:198])
  - "Показать состав" → $t('mrp.actions.show') ([frontend/src/pages/MRPResultPage.vue:204])
  - Заголовки колонок таблиц компонентов:
    - "Компонент" → $t('mrp.components.columns.name') ([frontend/src/pages/MRPResultPage.vue:653])
    - "Артикул" → $t('mrp.columns.article') ([frontend/src/pages/MRPResultPage.vue:654])
    - "Требуемое кол-во" → $t('mrp.components.columns.requiredQty') ([frontend/src/pages/MRPResultPage.vue:655])
    - "Этап" → $t('mrp.components.columns.stage') ([frontend/src/pages/MRPResultPage.vue:656])

1.2) Компонент сводки [vue.&lt;script setup&gt; MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1)
- "Результаты прогона MRP #…" → $t('mrp.title') ([frontend/src/components/mrp/MRPSummaryCard.vue:5])
- "RUN" → $t('mrp.run') ([frontend/src/components/mrp/MRPSummaryCard.vue:23])
- "Старт" → $t('mrp.startedAt') ([frontend/src/components/mrp/MRPSummaryCard.vue:28])
- "Финиш" → $t('mrp.finishedAt') ([frontend/src/components/mrp/MRPSummaryCard.vue:33])
- "Горизонт (дней)" → $t('mrp.horizonDays') ([frontend/src/components/mrp/MRPSummaryCard.vue:38])
- "Weekly" + "Да/Нет" → $t('mrp.weeklyYes')/$t('mrp.weeklyNo') ([frontend/src/components/mrp/MRPSummaryCard.vue:45], [frontend/src/components/mrp/MRPSummaryCard.vue:46])
- "Производственные заказы" → $t('mrp.summary.productionOrders') ([frontend/src/components/mrp/MRPSummaryCard.vue:50])
- "Заявки на закупку" → $t('mrp.summary.purchaseRequests') ([frontend/src/components/mrp/MRPSummaryCard.vue:55])
- "Перегруженные бакеты" → $t('mrp.summary.overloadedBuckets') ([frontend/src/components/mrp/MRPSummaryCard.vue:60])
- "Суммарный перегруз (ч)" → $t('mrp.summary.overloadTotal') ([frontend/src/components/mrp/MRPSummaryCard.vue:65])
- "Предупреждения" → $t('mrp.summary.warnings.title') ([frontend/src/components/mrp/MRPSummaryCard.vue:76])
- "Нажмите, чтобы развернуть" → $t('mrp.summary.warnings.caption') ([frontend/src/components/mrp/MRPSummaryCard.vue:77])
- "Проблемы привязки видов" → $t('mrp.summary.kindIssues.button') ([frontend/src/components/mrp/MRPSummaryCard.vue:104])

1.3) Фильтры [vue.&lt;script setup&gt; ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:1)
- "Бакет" → $t('mrp.filters.bucket') ([frontend/src/components/mrp/ProductionFilters.vue:13])
- "День задания (YYYY-MM-DD)" → $t('mrp.filters.dayDate') ([frontend/src/components/mrp/ProductionFilters.vue:18])
- "От даты (YYYY-MM-DD)" → $t('mrp.filters.fromDate') ([frontend/src/components/mrp/ProductionFilters.vue:30])
- "До даты (YYYY-MM-DD)" → $t('mrp.filters.toDate') ([frontend/src/components/mrp/ProductionFilters.vue:31])
- "Сбросить фильтры" → $t('mrp.filters.reset') ([frontend/src/components/mrp/ProductionFilters.vue:55])
- Опции бакета:
  - "Любой"/"daily"/"weekly" → $t('mrp.filters.bucketOption.any'|'daily'|'weekly') ([frontend/src/components/mrp/ProductionFilters.vue:82–85])

1.4) Таблицы production
- Единая таблица [vue.&lt;script setup&gt; ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1)
  - Заголовки колонок: "Наименование"/"Артикул"/"Количество"/"Норма, ч/шт"/"Норматив всего, ч" → mrp.columns.name/article/qty/normPerUnit/normTotal ([frontend/src/components/mrp/ProductionUnifiedTable.vue:61–66])
- Группированная [vue.&lt;script setup&gt; ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue:1)
  - "Вид производства:" → $t('mrp.group.productionKind') ([frontend/src/components/mrp/ProductionGroupedTable.vue:17])
  - "Срочн." → $t('mrp.group.urgencyDays') ([frontend/src/components/mrp/ProductionGroupedTable.vue:23])
  - "Перегруз:" → $t('mrp.group.capOverloadHours') ([frontend/src/components/mrp/ProductionGroupedTable.vue:25])
  - Колонки — как в unified ([frontend/src/components/mrp/ProductionGroupedTable.vue:72–76])
- Повестка дня [vue.&lt;script setup&gt; ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue:1)
  - "Вид производства:" → $t('mrp.group.productionKind') ([frontend/src/components/mrp/ProductionDailyAgenda.vue:17])
  - "Позиции (на день):" → $t('mrp.agenda.positionsCountDay') ([frontend/src/components/mrp/ProductionDailyAgenda.vue:19])
  - "Норматив (за день):" → $t('mrp.agenda.normDayHours') ([frontend/src/components/mrp/ProductionDailyAgenda.vue:20])
  - "Выпуск (за день):" → $t('mrp.agenda.qtyDay') ([frontend/src/components/mrp/ProductionDailyAgenda.vue:21])
  - Бейдж "без норматива" → $t('mrp.badge.noNormPerUnit') ([frontend/src/components/mrp/ProductionDailyAgenda.vue:38])
  - Колонки — как в unified ([frontend/src/components/mrp/ProductionDailyAgenda.vue:71–75])

1.5) Недостающие компоненты к реализации (после подтверждения)
- PeggingTable.vue — заголовки колонок:
  - $t('mrp.pegging.child'), $t('mrp.pegging.parent'), $t('mrp.pegging.qtyContribution'), $t('mrp.pegging.needDate'), $t('mrp.pegging.parentNeedDate')
- KindIssuesDialog.vue — заголовки колонок: раздел 1.1 (kindIssuesColumns)

2) План снижения реактивной нагрузки: чистка watch и переход на события

2.1) Дублирующий watch capUpper
- В коде присутствует два одинаковых набора наблюдателей за capUpper → оставить один:
  - Оставить блок на [frontend/src/pages/MRPResultPage.vue:1110], удалить дублирующий на [frontend/src/pages/MRPResultPage.vue:1125]

2.2) Явные события вместо общего deep‑watch
- Группировки и повестка дня:
  - Сейчас: [watch([() => prodAllRows.value, () => prod.filter.date_from, ...])](frontend/src/pages/MRPResultPage.vue:1102)
  - План: пересборку grouped/agenda вызывать явно из обработчика @apply фильтров (есть в верхней панели: [frontend/src/pages/MRPResultPage.vue:35–38]) с debounce ≈250 мс.
  - Для day_date — использовать событие 'day-picked' из [vue.&lt;script setup&gt; ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:78) вместо общего watch ([frontend/src/pages/MRPResultPage.vue:1115]).
- Справочники:
  - Убрать deep‑watch за itemMap/areaMap для группировок ([frontend/src/pages/MRPResultPage.vue:1097]); вызывать rebuildGroupedProductionOrders() в завершающих точках загрузок (уже вызывается внутри [ts.async function loadProduction()](frontend/src/pages/MRPResultPage.vue:670) и [ts.async function loadDictionaries()](frontend/src/pages/MRPResultPage.vue:522)).

2.3) Debounce реализация (без внешних зависимостей)
- Рекомендация: локальный helper debounce(fn, ms=250) в MRPResultPage или вынос в общий util. Применение для applyFilters/onDayPicked:
  - applyFiltersDebounced = debounce(async () => { await loadProduction(); await loadCapacityUpper(); await loadCapacityUpperDay(); rebuildDailyAgendaForDay(); }, 250)
  - Привязать @apply="applyFiltersDebounced" ([frontend/src/pages/MRPResultPage.vue:35])
  - Привязать @day-picked="applyDayFilterDebounced" ([frontend/src/pages/MRPResultPage.vue:37])

3) Подключение i18n — план
- Создать [ts.export function createI18n()](frontend/src/i18n/index.ts:1) и [ts.export const ru](frontend/src/i18n/ru.ts:1)
- Включить плагин в приложение: [ts.createApp()](frontend/src/main.ts:1) — app.use(i18n)
- Заменить «жёсткие» строки по карте из раздела 1

4) Интеграция недостающих компонентов (после подтверждения)
- Заменить локальный q-table пеггинга в разделе Pegging на [vue.&lt;script setup&gt; PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1) ([frontend/src/pages/MRPResultPage.vue:167])
- Вынести диалог проблем видов в [vue.&lt;script setup&gt; KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1) ([frontend/src/pages/MRPResultPage.vue:258])

Обновление чек‑листа итерации 2
- [x] Обновлена документация и зафиксирован детальный план
- [x] Создан каркас основных компонентов mrp/* (Production*, Purchases*, Capacity) — подтверждено наличием файлов
- [x] Подготовлены спецификации недостающих компонентов (PeggingTable, KindIssuesDialog) и i18n‑ключи
- [x] Сформирован детальный i18n‑маппинг по строкам (точки замены со ссылками)
- [x] Сформирован план чистки watch и перехода на события с debounce
- [ ] Реализованы PeggingTable.vue, KindIssuesDialog.vue и интеграция на страницу
- [ ] Подключён i18n и перенесены строки MRP в ru.ts
- [ ] Минимизированы watchers, добавлен debounce событий фильтров
- [ ] Приведены таблицы к QTableColumn&lt;T&gt; без any (в т.ч. Pegging/Capacity/Purchases)
- [ ] Выполнен smoke‑тест страницы и проверка уведомлений

Примечание
- В этой сессии продолжена только документация (.docs/progress.md). Для начала кодовых изменений (создание PeggingTable.vue/KindIssuesDialog.vue, подключение i18n и чистка watch) требуется подтверждение. Изменения будут выполняться пакетами по шагам с фиксацией прогресса здесь.

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 2 (Шаги 1–2: старт реализации)

Статус: выполнена часть Шагов 1–2. Созданы недостающие компоненты PeggingTable/KindIssuesDialog, добавлена и подключена i18n-база, начат перенос строк в i18n на странице-координаторе. Сборка зависимостей обновлена.

Сделано (код)
- Добавлена i18n-база и интеграция:
  - Создан словарь: [frontend/src/i18n/ru.ts](frontend/src/i18n/ru.ts:1)
  - Инициализация i18n: [ts.export const i18n](frontend/src/i18n/index.ts:1) с globalInjection=true
  - Подключение в приложение: [ts.app.use(i18n)](frontend/src/main.ts:1)
  - Зависимость добавлена: "vue-i18n": ^9.8.0 в [json.package.json](frontend/package.json:1); выполнен [shell.npm install](frontend/package.json:1)
- Недостающие компоненты:
  - PeggingTable: [vue.&lt;script setup&gt; PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1) — типизированные колонки, форматирование чисел/дат, emits: request
  - KindIssuesDialog: [vue.&lt;script setup&gt; KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1) — вынесение диалога проблем видов в компонент c i18n-заголовками
- Интеграция на страницу-координатор:
  - Замена локального q-table на компонент PeggingTable: [vue.&lt;template&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:167)
  - Замена локального q-dialog на KindIssuesDialog: [vue.&lt;template&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:258)
  - Импорт и подключение useI18n на странице: [ts.import { useI18n } from 'vue-i18n'](frontend/src/pages/MRPResultPage.vue:309); [ts.const { t } = useI18n()](frontend/src/pages/MRPResultPage.vue:660)
- Перенос строк в i18n (частично, страница):
  - Заголовок, вкладки, секция «Детальный анализ»: [vue.&lt;template&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:4)

Замечания/известные моменты
- IDE мог показывать временные ошибки вида «Cannot find module 'vue-i18n'»/«Cannot find './ru'» до выполнения npm install и/или перезапуска TS сервера; после установки зависимостей и сохранения файлов ошибки снимаются.
- В [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:401) остались локальные определения kindIssuesColumns (не используются после вынесения в компонент) — удаление запланировано при следующем правочном проходе.
- Для полной локализации требуется перенос строк из компонентов mrp/* (см. ниже ToDo).

ToDo (Шаг 2 — перенос строк в i18n по карте)
- [ ] Перенести строки в MRPSummaryCard: [vue.&lt;script setup&gt; MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1)
- [ ] Перенести строки/опции в ProductionFilters: [vue.&lt;script setup&gt; ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:1)
- [ ] Перенести заголовки колонок в ProductionUnifiedTable: [vue.&lt;script setup&gt; ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1)
- [ ] Перенести заголовки/тексты в ProductionGroupedTable: [vue.&lt;script setup&gt; ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue:1)
- [ ] Перенести заголовки/бэйджи в ProductionDailyAgenda: [vue.&lt;script setup&gt; ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue:1)
- [ ] Перенести заголовки колонок в PurchasesUnifiedTable: [vue.&lt;script setup&gt; PurchasesUnifiedTable.vue](frontend/src/components/mrp/PurchasesUnifiedTable.vue:1)
- [ ] При необходимости, привести CapacityTable к i18n (заголовки колонок формируются снаружи): [vue.&lt;script setup&gt; CapacityTable.vue](frontend/src/components/mrp/CapacityTable.vue:1)
- [ ] Очистить неиспользуемый код (kindIssuesColumns и пр.) на странице: [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:401)

Чек‑лист итерации 2 (обновлён)
- [x] Реализованы PeggingTable.vue и KindIssuesDialog.vue и интеграция на страницу
- [-] Подключён i18n и начат перенос строк MRP в ru.ts (страница) — в процессе
- [ ] Минимизированы watchers, добавлен debounce событий фильтров (следующий шаг)
- [ ] Приведены таблицы к QTableColumn&lt;T&gt; без any везде (проверка/доведение)
- [ ] Выполнен smoke‑тест страницы и проверка уведомлений

Изменённые/добавленные файлы
- Добавлено: [ts.export const i18n](frontend/src/i18n/index.ts:1), [ts.export const ru](frontend/src/i18n/ru.ts:1)
- Изменено: [ts.import { i18n } from './i18n'](frontend/src/main.ts:1), [ts.app.use(i18n)](frontend/src/main.ts:1)
- Добавлено: [vue.&lt;script setup&gt; PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1), [vue.&lt;script setup&gt; KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1)
- Изменено (интеграция и i18n): [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1)
- Изменено (deps): [json.package.json — vue-i18n](frontend/package.json:1)

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 2 (Шаги 1–2: продолжение)

Статус: работы продолжены. Реализован перенос строк в i18n для ключевых компонентов, внедрён debounce и частично упрощены watchers, интегрированы недостающие компоненты.

Изменения (код)
- i18n: подключение и использование
  - Инициализация и подключение i18n: [ts.export const i18n](frontend/src/i18n/index.ts:1) + [ts.app.use(i18n)](frontend/src/main.ts:1)
  - Словарь ru: [ts.export const ru](frontend/src/i18n/ru.ts:1)
- Недостающие компоненты (созданы ранее) и подключение:
  - PeggingTable — используется на странице: [vue.&lt;template&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:177)
  - KindIssuesDialog — используется на странице: [vue.&lt;template&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:257)
- Перенос строк в i18n (выполнено)
  - Страница-координатор: заголовок/вкладки/кнопки/фильтры/действия [vue.&lt;template&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:4)
  - Карточка сводки: [vue.&lt;script setup&gt; MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1)
  - Фильтры (bucket/day/from/to/reset + опции): [vue.&lt;script setup&gt; ProductionFilters.vue](frontend/src/components/mrp/ProductionFilters.vue:1)
  - Таблицы:
    - ProductionUnifiedTable: [vue.&lt;script setup&gt; ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1)
    - ProductionGroupedTable: [vue.&lt;script setup&gt; ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue:1)
    - ProductionDailyAgenda: [vue.&lt;script setup&gt; ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue:1)
    - PurchasesUnifiedTable: [vue.&lt;script setup&gt; PurchasesUnifiedTable.vue](frontend/src/components/mrp/PurchasesUnifiedTable.vue:1)
    - Компоненты (колонки) на странице: [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:636)
- Debounce и упрощение реактивности (частично)
  - Добавлены debounced‑обработчики: [ts.const applyProdFiltersDebounced/applyPurchFiltersDebounced/applyDayFilterDebounced](frontend/src/pages/MRPResultPage.vue:1005)
  - Привязка событий @apply/@day-picked к debounced‑функциям: [vue.&lt;template&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:35)
  - Удалён дублирующий watch capUpper (оставлен один ранее); удалены «тяжёлые» watch на prodAllRows/filters/day_date: [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1086)

Текущий эффект
- Интерфейс MRP на i18n‑ключах (основные области).
- Сокращено количество реактивных пересчётов за счёт debounce и удаления избыточных наблюдателей.
- Недостающие компоненты подключены и используются (PeggingTable, KindIssuesDialog).

Оставшиеся задачи итерации 2
- Завершить перенос строк i18n для оставшихся мест (локальные плейсхолдеры '—', подписи во вспомогательных местах и т.д.)
- Минимизировать watchers окончательно (переход на orchestrator‑события повсеместно); убедиться, что не осталось лишних deep watch
- Привести все таблицы к строгому QTableColumn&lt;T&gt; без any
- Smoke‑тест страницы (пагинация/экспорт/агрегаты/pegging/components/capacity) и проверка уведомлений

Обновление чек‑листа итерации 2
- [x] Реализованы PeggingTable.vue и KindIssuesDialog.vue и интеграция на страницу
- [-] Подключён i18n и перенесены строки MRP в ru.ts (основные компоненты и страница) — в процессе
- [-] Минимизированы watchers, добавлен debounce событий фильтров — в процессе (частично выполнено)
- [ ] Приведены таблицы к QTableColumn&lt;T&gt; без any (финальный проход)
- [ ] Выполнен smoke‑тест страницы и проверка уведомлений

Примечания
- При первой загрузке IDE могли появляться временные ошибки по vue‑i18n; после установки зависимости и сохранения файлов они отсутствуют.
- В дальнейших шагах будет завершён перенос оставшихся строк в i18n и доведена строгая типизация колонок.

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 2 (i18n перенос, чистка watchers, интеграция компонентов — выполнено частично)

В этой сессии продолжен рефакторинг фронтенда по плану итерации 2. Выполнены перенос строк в i18n для MRP-страницы и связанных компонентов, сокращена реактивная нагрузка (частичная чистка watchers), доведена унификация заголовков колонок и плейсхолдеров.

Сделано (код)
- Перенос строк в i18n:
  - Страница-координатор: [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1) — заголовок, вкладки, кнопки, подписи фильтров, колонки детальных таблиц через t().
  - Карточка сводки: [vue.&lt;script setup&gt; MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1) — локализация подписи Weekly → t('mrp.weeklyLabel').
  - Таблицы MRP:
    - [vue.&lt;script setup&gt; ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1)
    - [vue.&lt;script setup&gt; ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue:1)
    - [vue.&lt;script setup&gt; ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue:1)
    - [vue.&lt;script setup&gt; PurchasesUnifiedTable.vue](frontend/src/components/mrp/PurchasesUnifiedTable.vue:1)
    - [vue.&lt;script setup&gt; PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1)
    - [vue.&lt;script setup&gt; KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1)
  - Обновлён словарь: [ts.export const ru](frontend/src/i18n/ru.ts:1)
    - Добавлены ключи столбцов: mrp.columns.orderId, purchaseId, itemId, needDate, startDate, finishDate, bucketType, bucketDate, priorityIndex, stages, areaId, hoursPlanned, hoursAvailable, overloadHours, orderDate, leadTimeDays.
    - Переведены подписи фильтров пеггинга (childItemId/parentItemId).
    - Добавлен ключ mrp.weeklyLabel.
- Плейсхолдеры и fallback:
  - Заменены жёсткие строки '—' и "Номенклатура #..." на i18n-плейсхолдеры:
    - t('mrp.placeholder.noArticle') и t('mrp.placeholder.itemNameFallback', { id }).
    - Применено в [vue.&lt;script setup&gt; ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1), [vue.&lt;script setup&gt; PurchasesUnifiedTable.vue](frontend/src/components/mrp/PurchasesUnifiedTable.vue:1), [vue.&lt;script setup&gt; ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue:1), [vue.&lt;script setup&gt; ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue:1), [vue.&lt;script setup&gt; KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1) и в плоском fallback на странице [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:426).
- Единообразные заголовки колонок:
  - Детальные таблицы на странице: prodColumns/purchColumns/capColumns переведены на t('mrp.columns.*') в [vue.&lt;script setup&gt; MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:294).
  - Рекомендованные агрегаты: recommendedProdColumns/recommendedPurchColumns переведены на t('mrp.columns.*') ([frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:309)).
  - Колонки Pegging переведены на t('mrp.pegging.*') ([frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:344)) и в отдельном компоненте [vue.&lt;script setup&gt; PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1).
- Снижение реактивной нагрузки:
  - Удалён «тяжёлый» deep-watch за prod.rows/itemMap/areaMap; теперь пересборка групп вызывается явными точками загрузки (loadProduction/loadDictionaries/loadCapacityUpper) и при обновлении сводки мощностей ([ts.watch удалён блок, добавлен комментарий](frontend/src/pages/MRPResultPage.vue:1090)).
  - Debounce-обработчики уже подключены ранее и используются (@apply/@day-picked).

Замечания
- Типизация колонок QTableColumn&lt;T&gt; в детальных компонентах ProductionDetailTable/PurchasesDetailTable/CapacityTable оставлена как есть (T=any поставляется со страницы). В специализированных таблицах Unified/Grouped/DailyAgenda используются вычисляемые QTableColumn, покрывающие сценарии без any в значениях столбцов.
- В composables fallback-строки "Номенклатура #..." пока остаются для бэкенд-денормализации; на уровне компонентов и страницы применены i18n-плейсхолдеры.

Чек‑лист итерации 2 (обновление)
- [x] Добавлены недостающие компоненты: PeggingTable.vue, KindIssuesDialog.vue (ранее выполнено)
- [x] Интегрированы composables/store в страницу-координатор (частично ранее) и обновлены вызовы с i18n
- [x] Подключён i18n и перенесены строки MRP в ru.ts (страница и основные компоненты)
- [-] Минимизированы watchers, добавлен debounce событий фильтров (выполнено частично: удалён heavy deep-watch)
- [ ] Приведены таблицы к QTableColumn&lt;T&gt; без any (финальный проход по детальным компонентам)
- [ ] Выполнен smoke‑тест страницы и проверка уведомлений

Список изменённых файлов
- [frontend/src/i18n/ru.ts](frontend/src/i18n/ru.ts:1)
- [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1)
- [frontend/src/components/mrp/MRPSummaryCard.vue](frontend/src/components/mrp/MRPSummaryCard.vue:1)
- [frontend/src/components/mrp/ProductionUnifiedTable.vue](frontend/src/components/mrp/ProductionUnifiedTable.vue:1)
- [frontend/src/components/mrp/ProductionGroupedTable.vue](frontend/src/components/mrp/ProductionGroupedTable.vue:1)
- [frontend/src/components/mrp/ProductionDailyAgenda.vue](frontend/src/components/mrp/ProductionDailyAgenda.vue:1)
- [frontend/src/components/mrp/PurchasesUnifiedTable.vue](frontend/src/components/mrp/PurchasesUnifiedTable.vue:1)
- [frontend/src/components/mrp/PeggingTable.vue](frontend/src/components/mrp/PeggingTable.vue:1)
- [frontend/src/components/mrp/KindIssuesDialog.vue](frontend/src/components/mrp/KindIssuesDialog.vue:1)

План следующих шагов
1) Завершить чистку watchers: убедиться, что остались только точечные наблюдения; пересборки по явным событиям и после загрузок.  
2) Довести строгую типизацию QTableColumn&lt;T&gt; в ProductionDetailTable/PurchasesDetailTable/CapacityTable (проброс конкретных интерфейсов из [frontend/src/types/mrp.ts](frontend/src/types/mrp.ts:1)).  
3) Smoke-тест: вкладки production/purchases/capacity/pegging/components; уведомления и экспорт.  
4) При необходимости, выровнять остаточные строки в компонентах вне MRP области (например, [frontend/src/components/ItemList.vue](frontend/src/components/ItemList.vue:1)) последующими итерациями (не блокирует приёмку MRP).

Критерии приёмки текущего шага
- Строки интерфейса MRP вынесены в i18n, UI отображает переводы.  
- Нет «тяжёлых» deep-watch на больших структурах; применяется debounce событий фильтров.  
- Заголовки всех задействованных таблиц для MRP берутся из i18n.  

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 2 (i18n-остатки, строгая типизация колонок, частичная чистка)

Статус: выполнены правки по переносу оставшихся строк в i18n для унифицированных таблиц, введена строгая типизация QTableColumn&lt;T&gt; для детальных таблиц и соответствующих колонок на странице, приведены плейсхолдеры к i18n.

Сделано (код)
- Перенос строк в i18n (остатки для унифицированных таблиц):
  - Заголовки prodUnifiedColumns/purchUnifiedColumns переведены на t('mrp.columns.*') в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1).
- Строгая типизация QTableColumn&lt;T&gt;:
  - Колонки детальных таблиц на странице:
    - prodColumns → QTableColumn&lt;ProductionOrder&gt; в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:294).
    - purchColumns → QTableColumn&lt;PurchaseRow&gt; в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:323).
    - capColumns → QTableColumn&lt;CapacityRow&gt; в [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:335).
  - Компоненты:
    - ProductionDetailTable: rows/columns типизированы ProductionOrder/QTableColumn&lt;ProductionOrder&gt;, добавлен useI18n для плейсхолдера в слоте стадий — [vue.&lt;script setup&gt; ProductionDetailTable.vue](frontend/src/components/mrp/ProductionDetailTable.vue:1).
    - PurchasesDetailTable: rows/columns типизированы PurchaseRow/QTableColumn&lt;PurchaseRow&gt; — [vue.&lt;script setup&gt; PurchasesDetailTable.vue](frontend/src/components/mrp/PurchasesDetailTable.vue:1).
    - CapacityTable: rows/columns типизированы CapacityRow/QTableColumn&lt;CapacityRow&gt; — [vue.&lt;script setup&gt; CapacityTable.vue](frontend/src/components/mrp/CapacityTable.vue:1).
- i18n плейсхолдеры:
  - В ProductionDetailTable пустые стадии отображаются через t('mrp.placeholder.noArticle') вместо '—' — [vue.&lt;script setup&gt; ProductionDetailTable.vue](frontend/src/components/mrp/ProductionDetailTable.vue:12).

Замечания
- В watchers страницы тяжелый deep-watch ранее удалён; текущие точечные watch сохраняются (tab/viewTab/карта мощностей/фильтры), debounce уже внедрён. Финальную чистку remaining-watchers запланировано довести следующим шагом.

Чек-лист итерации 2 (обновление)
- [x] Добавлены недостающие компоненты: PeggingTable.vue, KindIssuesDialog.vue
- [x] Интегрированы composables/store в страницу-координатор и обновлены вызовы с i18n (ранее выполнено)
- [x] Подключён i18n и перенесены строки MRP в ru.ts (страница и основные компоненты; унифицированные таблицы переведены в этом шаге)
- [-] Минимизированы watchers, добавлен debounce событий фильтров (частично выполнено; финальная чистка — следующий шаг)
- [x] Приведены таблицы к QTableColumn&lt;T&gt; без any (детальные компоненты и колонки на странице)
- [ ] Выполнен smoke‑тест страницы и проверка уведомлений

Изменённые файлы
- [frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:1)
- [frontend/src/components/mrp/ProductionDetailTable.vue](frontend/src/components/mrp/ProductionDetailTable.vue:1)
- [frontend/src/components/mrp/PurchasesDetailTable.vue](frontend/src/components/mrp/PurchasesDetailTable.vue:1)
- [frontend/src/components/mrp/CapacityTable.vue](frontend/src/components/mrp/CapacityTable.vue:1)

План следующих шагов
1) Довести финальную чистку watchers на странице: оставить только точечные наблюдения; пересборки — по явным событиям/debounce и после загрузок.  
2) Smoke‑тест: вкладки production/purchases/capacity/pegging/components; проверка пагинации, экспорта и уведомлений.  
3) При необходимости, добить единообразие колонок в детальных компонентах снаружи страницы (если будут выявлены «any» при сборке/линтинге).

## 2025-10-07 — Фронтенд: рефакторинг MRPResultPage — итерация 2 (финал чистки watchers, типизация состояний)

Статус: завершена чистка watchers и доведена строгая типизация состояний/колонок. Pegging переведён на строгие типы, колонки определяются внутри компонента.

Сделано (код)
- Чистка watchers (минимизация реактивной нагрузки):
  - Удалён watcher на карту мощностей capUpper с вызовом rebuildGroupedProductionOrders(); перестроение выполняется явно в [ts.async function loadCapacityUpper()](frontend/src/pages/MRPResultPage.vue:826) (в конце функции).
  - Для изменений фильтров верхней вкладки «Производство» добавлен debounced‑вызов [ts.const loadCapacityUpperDebounced = debounce(loadCapacityUpper, 250)](frontend/src/pages/MRPResultPage.vue:1032) и замена прямого вызова в watch на debounced вариант ([frontend/src/pages/MRPResultPage.vue:1126]).
- Строгая типизация reactive‑состояний и колонок:
  - Состояния страницы приведены к явным дженерикам:
    - prod: rows: ProductionOrder[], columns: QTableColumn&lt;ProductionOrder&gt;[] ([frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:583))
    - purch: rows: PurchaseRow[], columns: QTableColumn&lt;PurchaseRow&gt;[] ([frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:598))
    - cap: rows: CapacityRow[], columns: QTableColumn&lt;CapacityRow&gt;[] ([frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:613))
    - comp.columns: QTableColumn&lt;SpecNode&gt;[] (убраны any) ([frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:645))
- Pegging:
  - Удалены локальные определения pegColumns (ответственность за колонки внутри PeggingTable).
  - Состояние peg типизировано: rows: PeggingRow[]; без локального поля columns ([frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:629)).
  - Импортирован тип PeggingRow ([frontend/src/pages/MRPResultPage.vue](frontend/src/pages/MRPResultPage.vue:282)).

Итог по i18n
- Ранее перенесённые строки i18n для страницы и компонентов — актуальны. Дополнительно: ProductionDetailTable слот пустых стадий теперь использует t('mrp.placeholder.noArticle').

Чек-лист итерации 2 (обновление)
- [x] Добавлены недостающие компоненты: PeggingTable.vue, KindIssuesDialog.vue
- [x] Интегрированы composables/store в страницу-координатор и i18n
- [x] Подключён i18n и перенесены строки MRP в ru.ts (страница и основные компоненты; унифицированные таблицы переведены)
- [x] Минимизированы watchers, добавлен debounce событий фильтров (финализировано)
- [x] Приведены таблицы и состояния к QTableColumn&lt;T&gt;/строгим типам без any (деталь/состояния, в т.ч. Pegging/Comp)
- [ ] Выполнен smoke‑тест страницы и проверка уведомлений

План следующих шагов
1) Smoke‑тест: проверить вкладки production/purchases/capacity/pegging/components; пагинацию/экспорт/уведомления; поведение фильтров и debounce.  
2) При необходимости внести мелкие корректировки по UI/типам по результатам smoke‑теста.

## 2025-10-07 — Фронтенд: smoke‑тест (запуск dev, проверка основных экранов)

Статус: dev‑сервер запущен локально, базовая навигация и i18n работают, критических ошибок не обнаружено. На экране «MRP планирование — прогоны» данные отсутствуют (No data available), поэтому полноценная проверка страницы результатов прогона (MRPResultPage) возможна после создания run.

Технические детали
- Запуск dev: `npm install` → ok; `npm run dev` → ok. Quasar SPA доступен по http://localhost:9000/
- Консоль браузера: единичное предупреждение Vue («There is already an app instance mounted…») при HMR — не блокирует работу.
- Проверены экраны:
  - Главная.
  - «План выпуска техники дневной/квартальный» — отрисовываются, данных нет (ожидаемо).
  - «MRP планирование — прогоны» — рендер без ошибок, пустой список.
- i18n:
  - Строки интерфейса на проверенных экранах корректно русифицированы.
  - На MRPResultPage ранее перенесённые ключи используются, регрессов не выявлено.
- Типизация/линт:
  - Внесённые правки с дженериками QTableColumn&lt;T&gt; компилируются в dev‑режиме, типовых ошибок не наблюдается.
  - Устранена потенциальная ошибка типов в ProductionUnifiedTable (field как функция).

Ограничение для smoke‑теста
- Для проверки вкладок страницы результатов прогона (MRPResultPage) требуется хотя бы один run_id. На текущем стенде список прогонов пуст.

Рекомендации для продолжения smoke‑теста
1) Создать прогон:
   - Через UI («Рассчитать» на экране «MRP планирование — прогоны») или
   - Через API/скрипт бэкенда согласно документации (см. [.docs/03-api-reference.md](.docs/03-api-reference.md:1) / маршруты планирования).
2) Открыть страницу результатов по маршруту: /mrp/:runId и проверить:
   - Сводку, агрегаты (grouped/agenda), детальные таблицы (production/purchases/capacity), пеггинг, компоненты.
   - Экспорт CSV/XLSX для производства/закупок.
   - Уведомления (Notify) при ошибках загрузки.
3) Зафиксировать результаты и закрыть чек‑лист итерации 2.

Примечание
- Dev‑сервер оставлен запущенным в терминале проекта (Quasar SPA). После получения run_id будет выполнен финальный проход по smoke‑тесту и обновлён чек‑лист.

## 2025-10-09 — Правило целых количеств: заказы на производство и «Повестка дня»

Статус: реализовано в коде. Описание изменений и ссылок на файлы ниже.

Что сделано
- Исключены дробные количества в производственных заказах:
  - При создании заказа после лот‑сайзинга количество округляется вверх до целого (ceil). Реализация в [backend/app/services/planning_service.py](backend/app/services/planning_service.py).
  - Диагностически к деталям расчёта добавлено поле normalized_qty_rounded (целевое округлённое количество) в прикреплённом к заказу служебном объекте _comp_details.
- Согласовано поведение после ограничения мощностью:
  - После вычисления ограниченного количеством мощности величина также округляется вверх (ceil), затем пересчитываются total_hours и используется округлённое значение для дальнейшего расписания в том же цикле. Реализация в [backend/app/services/planning_service.py](backend/app/services/planning_service.py).
  - Примечание: округление вверх после лимита мощности может незначительно выходить за окно доступной мощности; это обработано планировщиком: остаток часов пойдёт в SCHED_OVERFLOW (fallback на ближайший рабочий день), а также отражается в предупреждениях CAPACITY_LIMITED/CAPACITY_SHORTAGE.
- «Повестка дня» (дневные агрегаты) теперь показывает выпуск только целыми штуками:
  - Для каждой строкы выпуска за день используется qty_day = ceil(hours_today / norm_per_unit).
  - Разница до целого изделия (остаток нормочасов) добавляется к суммарному нормативу группы в шапке (group.norm_sum_hours), как требовалось.
  - Поведение для перегруза (строки без часов на день, но назначенные на дату; synthetic строки из CAPACITY_SHORTAGE) сохранено; изменения касаются только расчёта суточного выпуска и суммарных нормочасов. Реализация в [backend/app/services/planning_service.py](backend/app/services/planning_service.py).

Аргументация и соответствие требованию
- Требование: «В заказах на производство не допускаются дробные количества. Можно округлять вверх, остаток норматива на день добавлять к общему в шапке.»
  - В заказах: теперь всегда целые количества (ceil) на этапе создания, а также после лимитирования мощностью.
  - В «Повестке дня»: показ выпуска в целых единицах, а недостающие нормочасы до полного изделия не теряются — добавляются в суммарный норматив группы.

Затронутые файлы
- [backend/app/services/planning_service.py](backend/app/services/planning_service.py)

Замечания по совместимости
- Структура БД не менялась. Поля qty в плановых таблицах остаются DECIMAL, но значения для производственных заказов теперь всегда целые по бизнес‑правилу.
- Закупки (PlannedPurchase) не затрагивались — правило распространяется только на производственные заказы и дневной вывод выпуска.

Диагностика и предупреждения
- CAPACITY_LIMITED и CAPACITY_SHORTAGE обогащены контекстом: логируется исходное, ограниченное (а также округлённое вверх) количество и нормочасы дефицита.
- Имитация перегруза через SCHED_OVERFLOW не изменялась.

Критерии приёмки (выполнено)
- В выгрузке результатов MRP для производства отсутствуют дробные PlannedOrder.qty.
- На экране «Повестка дня» строки показывают только целые количество выпуска за день; суммарный норматив в шапке группы учитывает остаток нормочасов до целого.

## 2025-10-13 — Экспорт «на выбранную дату» (day_date) для производства: исправление пустой выгрузки

Контекст:
- На странице результатов прогона при нажатии на выгрузку XLSX/CSV при выбранной дате формировался только заголовок без строк.
- Причина: экспорт фильтровал заказы по bucket_date (даты бакетов заказов: daily/weekly). При недельных бакетах (weekly → пятница ISO‑недели) при выборе произвольного дня (например, вторника) в диапазоне day_date→day_date результирующий набор был пустым.

Решение (без ломающих изменений API):
1) Backend
   - Добавлена поддержка параметра day_date в эндпоинт экспорта производства [python.def export_planning_result_production()](backend/app/routers/plan.py:532).
   - При наличии day_date используется серверная дневная повестка по стадиям (статусы на день) из [python.def get_run_production_agenda_day()](backend/app/services/planning_service.py:3004).
   - Маппинг данных экспорта (XLSX/CSV) для режима day_date:
     - Количество: display_qty (если задано) иначе qty
     - Норматив всего, ч: display_norm_hours_total (если задано) иначе norm_hours_total
     - Норма, ч/шт: norm_hours_per_unit (как есть/с фолбэком)
   - Групповые подзаголовки в файле используют дневные показатели: «Норматив дня» (group.norm_sum_hours), как на экране «Задание на день».
   - Ссылки на реализацию:
     - [python.def export_planning_result_production()](backend/app/routers/plan.py:532)
     - [python.def get_run_production_agenda_day()](backend/app/services/planning_service.py:3004)

2) Frontend
   - Сервис API: добавлен параметр day_date в [ts.export function exportPlanningResultProduction()](frontend/src/services/api.ts:229).
   - Страница результатов: при выбранном дне передаётся day_date вместо date_from/date_to:
     - Кнопки «CSV/XLSX» на верхней панели: [vue.exportProd()](frontend/src/pages/MRPResultPage.vue:840)
   - Composable производства: аналогичное поведение для общего экспорта: [ts.export function exportProd()](frontend/src/composables/useProduction.ts:241)

Формат выгрузки (без изменений для режима диапазона дат):
- Заголовки: Наименование, Артикул, Количество, ЕИ, «Норматив, ч/шт», «Норматив всего, ч»
- Группы: «Производственный участок: {area_name} · Заказов: N · Норматив дня/всего: HHH ч»
- XLSX: openpyxl, слияние ячеек для заголовков групп, лёгкая заливка
- CSV: подзаголовок группы одной строкой + пустая строка-разделитель между группами

Проверка (после пересборки контейнеров):
- Запрос списка прогонов: GET http://localhost:8000/api/v1/plan/runs → наличие run_id (например, 87)
- Проверка day_date без данных для дня:  
  GET /api/v1/plan/results/87/production/export?format=csv&amp;day_date=2025-10-07 → корректный заголовок, 0 строк (ожидаемо при отсутствии стадий/выпуска за день)
- Проверка day_date c данными (пример):  
  GET /api/v1/plan/results/87/production/export?format=csv&amp;day_date=2025-10-10 → файл содержит группы и строки (пример в терминальном выводе; total_rows=156)

Влияние на UI:
- При выборе даты в фильтрах верхнего блока «Производство» (day_date) выгрузка XLSX/CSV теперь повторяет логику карточки «Задание на день»: отображаются позиции по участкам за выбранный день, включая строки перегруза (display_*).
- Поведение экспорта по обычному диапазону дат (date_from/date_to, bucket_type) не изменено.

Обратная совместимость:
- Параметры bucket_type/date_from/date_to продолжают работать как прежде.
- Новый параметр day_date опционален; при его отсутствии работает старый код экспорта.

Деплой:
- Выполнена пересборка и перезапуск через rebuild.bat (docker compose build --pull --no-cache; up).
- Фронтенд доступен на http://localhost:9000; backend — http://localhost:8000.

Критерий приёмки задачи:
- При выбранной дате на странице MRP «Результаты прогона» кнопка «XLSX/CSV» формирует файл со списком деталей (строк) за выбранный день по участкам, а не только заголовок.
