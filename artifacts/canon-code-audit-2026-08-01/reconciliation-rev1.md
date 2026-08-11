# Реестр разночтений «канон → код» — 01.08.2026

Аудит рабочего дерева `repo/` (ветка `codex/repair-ledger-rebuild`, 140 незакоммиченных
файлов, +7782/−8625). Направление сверки: **канон → код**. Код, противоречащий
документации, квалифицирован как легаси-дефект, а не как замысел.

Источники нормы: `.docs/CANON.md`, `.docs/notes/mrp-decisions-log.md` (§N),
контракты `.docs/*.md`. Восемь параллельных аудитов по доменам.

Тесты не запускались (аудит только на чтение), кроме `canon-invariants` (102 passed)
и трёх OpenAPI/schema-тестов (20 passed). Полный pytest волны не снимался.

---

## 0. БЛОКЕРЫ — вопросы владельцу (разрешать молча запрещено)

| # | Расхождение | Позиции | Вердикт нужен |
|---|---|---|---|
| Б-1 | `pull_qty`: формула `min(gap, unlaunched)` (`shelves-…md:134`, `CANON.md:44`) против прозы «сначала перемещение» + инвариант 6 (`:145-147, :198`). Код реализует прозу: `pull = min(max(gap − transfer, 0), unlaunched)` | `shelf_projection_core.py:145-149` | какая версия нормативна |
| Б-2 | Custody не имеет владельца в реестре `CANON.md`, хотя `db_schema.md:107-108` описывает три новых таблицы; фактически два вычислителя с дефолтом на живой | `production_material_custody*.py` | кто владелец; допустим ли живой путь в командных операциях |
| Б-3 | `assembly-queue-and-drum.md` не говорит, чей `period_from` — `PlanningRun` или `ProductionPlanHeader`. Код выбрал по-разному в трёх местах | см. Д-2 | какой период канонический |
| Б-4 | §18 записан как описание кода («текущий код выделяет Переработку в третий flow») — направление code→canon, запрещённое `CLAUDE.md`. И уже устарел: в ядре резервов Переработка = `make` | `reservation_ledger.py:142-150` | судьба Переработки + переписать §18 |
| Б-5 | `Document_СборкаЗапасов` создаётся без `ДокументОснование`/`_Type`; доки требуют. В соседнем случае (окраска→сварка) код шлёт оба поля | `one_c_manufacture_export.py:643-651` | правка кода или правка доки |
| Б-6 | Нумерация: `PP`/`PO` соответствуют, `MT`/`RT`/`MF`/`PW` — нет (локальные id вместо ключа цепочки `RRRROOOOO`). Код ссылается на лимит `Number`=11 | `one_c_document_numbers.py:22-41` | правка кода или правка доки |
| Б-7 | Запись в `Catalog_Спецификации` легализована канон-тестом, но отсутствует в матрице направлений | `spec_writeback_1c.py:164-170` | внести в матрицу или снять |
| Б-8 | §12.3 разрешает закрытие заказа кнопкой оператора через санкционированный экспорт — такой точки в коде нет вообще | — | закрыть функционал или снять из §12.3 |
| Б-9 | `.docs/progress.md:36-40` фиксирует отменённую модель («frontend содержит предметные вычисления… DBR-дефицитов»); проверено — во фронте этого нет, DBR-экраны удалены | `.docs/progress.md` | удалить/переписать |

---

## 1. P0 — ДВОЙНОЙ УЧЁТ ФИЗИЧЕСКОГО КОЛИЧЕСТВА

Три независимых пути, каждый нарушает инвариант «один физический факт учитывается один раз».

### A-1. §16 не реализован — корень «MRP stock double-count»
`senior_hold_qty` / `accepted_attributed_consumption_qty` / `free_s0_qty` отсутствуют
в коде целиком (0 совпадений по `backend/`; есть только в доках и в текстовой
проверке `test_canon_invariants.py:160-174`).

Вместо динамического удержания `mrp_freeze.py:925-978` исключает из пула статичное
`covered_from_stock_at_freeze_qty` активной freeze-версии.

Сценарий: старший план резервирует 100, покрыто складом 60, пополнение 40 пришло →
физический остаток 100. Канон: `free_s0 = max(100 − 100, 0) = 0`. Код исключает 60 и
отдаёт новому плану 40 — единицы, заказанные под старший план.

**Обратная сторона:** `mrp_freeze.py:970-978` бросает `LedgerPoolUnavailable`, когда
замороженный claim превышает текущий SLE-баланс. Нормальное исполнение старого плана
блокирует фиксацию любого нового плана по номенклатуре. Пути восстановления нет
(`refreeze_active_snapshots` изъят). Закреплено `test_mrp_freeze_candidate.py:312-384`.

Подтверждено двумя аудитами независимо.

### A-2. §6 адресность не реализована
`historical_replay_core.py:17` — `MatchRule = Literal["fifo"]`, единственное значение.
Ядро строит список `compatible` по совпадению пула и вызывает `place(..., "fifo")` один
раз. Шага `addressed_take = min(qty, остаток связанного живого резерва)` нет.

`_identity_for_sle` (`persistence:90-149`) разрешает `requirement_id`/`order_ref` и кладёт
в `Fact` — **ядро эти поля не читает никогда**. Ветка `"pegged"` (`persistence:368`)
недостижима.

**Отменённая модель закреплена в двух местах:**
- докстринг `core:139-141`: «Requirement/order identity is provenance only and never
  gates quantity» — эту фразу `test_canon_invariants.py:135-146` объявляет retired,
  но сканирует только `.docs/`, не код;
- тест `test_historical_replay_core.py:73-92` **требует**, чтобы факт с
  `requirement_id=20` ушёл в чужой старейший резерв, а не в связанный.

Правка по §6 «сломает тесты» и будет откачена следующей волной — точная механика
инцидента 26-30.07.

Последствие: связанный резерв остаётся незакрытым → оператор дозаказывает поступившее.

### A-3. Перенос future supply тащит чужой cutoff
`future_supply_capture.py:316-389` (`carry_forward_future_supply`): при каждом
физическом refresh (цикл 3 ч) строки `ledger_future_supply` копируются из родителя
дословно списком `_CARRY_FORWARD_FIELDS:271-292`, включая `capture_cutoff` и
`open_qty_at_cutoff`, замеренный на **старом** cutoff.

Тот же модуль на пути честного захвата требует обратного: `_validated_rows:203-204` —
«evidence capture_cutoff must exactly equal generation cutoff». Carry-forward этот
валидатор не проходит. `test_physical_refresh_future_supply.py:130-133` фиксирует
расхождение как ожидаемое.

Поступление между родительским и новым cutoff попадает в `stock_bin.on_hand` нового
поколения **и** остаётся в `open_qty_at_cutoff`.

Потребители: `purchase_control_snapshot.py:261,280-281,468`,
`shelf_projection_persistence.py:188-198`, `mrp_freeze.py:275,312-313,331-332`,
`reservation_ledger.py:397`, `production_control_material_availability.py:118-127`.

Нарушены инварианты 2 и 4 контракта истины.

### A-4. Custody-фолд невоспроизводим (молчаливая потеря дельт)
`production_material_custody_projection.py:166-176`: watermark =
`max(id) WHERE effective_at <= cutoff`, а фолд (`:298-301`) идёт по `id`.

`effective_at` немонотонен относительно `id` **по построению**:
`custody_events.py:248` ставит `effective_at = sle.posting_at` (1С проводит задним
числом), `:129` для терминальных операций — `datetime.now()`. Ограничения монотонности
в схеме нет (`20260731_05_custody_projection.py:17-101`).

Событие с `id ≤ baseline_watermark`, но поздним `effective_at`, не попадёт ни в один
фолд никогда. Fail-closed не срабатывает — ветка `:368-375` ловит только знак.
Тестами не покрыто.

В остальном модуль — самый строгий fail-closed в кодовой базе (14 точек отказа).

---

## 2. P0 — МОЛЧАЛИВАЯ ПОТЕРЯ / ПОДМЕНА ФАКТА

### B-1. Факты выбрасываются мимо проверки сохранения количества
`historical_replay_persistence.py:291-316`: причины исключения
`warehouse_policy_missing` (в базе нет ни одной `StockWarehouse`) и пустой
`warehouse_ref1c` молча выкидывают факт — он не попадает ни в `facts`, ни в `fact_qty`,
ни в `surplus`. Поэтому чек `fact_qty == allocated + surplus`
(`generation_lifecycle.py:896-899`) его не видит и **публикация проходит**.

При пустой таблице складов исключаются все make-факты → выполнение публикуется как
**0 вместо `unavailable`**.

### B-2. Валидатор закупочной строки пропускает NULL как ноль
`purchase_control_snapshot.py:80-84`:
```python
def _to_float(value):
    try: return float(value or 0)
    except (TypeError, ValueError): raise ValueError("numeric field is malformed")
```
`value or 0` выполняется **до** конверсии — `None`/`""` до `raise` не доходят.
Единственный числовой гейт в `validate_purchase_control_journal_buy_row`, вызываемый
публикатором (`obligation_refresh_publish.py:849`). Строка со всеми NULL проходит все
`isclose`-сверки как самосогласованный нуль и вмораживается со статусом `available`.

Тот же шаблон: `production_control_common.py:26-30` (`except Exception: return 0.0`) →
компонент с битой нормой молча исчезает из обеспеченности
(`production_control_material_availability.py:68-70`), агрегат по выжившим даёт
«Обеспечен». И `production_material_custody_events.py:82-84` — битая дельта становится
«движения не было», событие теряется из append-only ленты.

### B-3. Неоднозначная связь роняет всё поколение вместо FIFO
`generation_lifecycle.py:903-906`: `if ambiguous_identity_facts != 0: raise`.
Канон (§6, planning-truth-contract) явно требует: факт с неоднозначной связью
**целиком распределяется FIFO**. Один факт с двумя кандидатами-заказами делает истину
недоступной целиком.

### B-4. Фронт показывает ноль вместо неизвестного — системно
`lib/format.ts:1-3`: `qty(value) = Number(value || 0).toLocaleString(...)`.
Любой `null`/`unavailable` рендерится «0» на MRP-итогах, н/ч, остатках, матрице плана.
Рядом `fieldFormat.ts:39-40` для того же типа возвращает `'—'` — правильное поведение
в каркасе есть, страницы зовут `qty()` напрямую.

Худший случай: `ProductionDetailPane.tsx:56,205` — `mrp_req_remaining_qty ?? 0`, затем
`<= 0.001` → бейдж **«Потребность закрыта полностью»**. При недоступном факте UI
утверждает противоположное.

Ещё: `?? 0` управляет решением «снимок не зафиксирован»
(`purchaseOrdersDoctype.ts:154`) — работает только потому, что 0 falsy.

### B-5. Обеспеченность/ETA не проверяет capability `future_supply`
`production_control_material_availability.py:269,423` — `require_accepted_truth(...)`
без `required_capabilities`. Эталон рядом: `routers/item_ledger.py:255-264` гейтит на
`CAPABILITY_FUTURE_SUPPLY` с докстрингом «never as zero ordered / zero in transit».

На поколении без захвата `future_supply_eta.get(iid, [])` даёт `[]` («поставок не
ожидается»), `incoming` схлопывается в `0.0` — и вмораживается в принятый снимок.

---

## 3. P0 — ОБХОД КАНОНИЧЕСКОГО ЗАПРЕТА

### C-1. Количество материализованной строки переписывается через 1С-синк
Канон запрещает прямое перезаписывание количества исполнительной строки; ради этого
удалён `dedupe_mrp_production_orders.py`, и канон-тест механически проверяет отсутствие
ручек правки. **Проверено: публичного API нет, скрипт удалён без остатков.**

Но `production_order_sync.py:539-551` делает `existing_product.quantity = quantity`
из 1С **безусловно**, без проверки `order.source == "1c"`. MRP-заказ после экспорта
получает `order_ref1c` (`one_c_production_order_export.py:909,924`), ночной синк
находит его по `Ref_Key` и переписывает обязательство.

Правка оператора в 1С меняет обязательство PRODPLAN без нового поколения.

### C-2. Локальные заказы закрываются по отфильтрованной выгрузке
`production_order_sync.py:590-607`: «отсутствует в загрузке ⇒ `deletion_mark = True`».
Загрузка при этом:
- отфильтрована сервером `Date ge datetime'2026-05-01' and Posted eq true` — оператор
  **распровёл** заказ в 1С, документ жив, но исчез из выборки;
- отфильтрована в коде: `if rec.get("СостояниеЗаказа_Key") is None: continue` (`:279-281`);
- может быть **обрезана**: `odata_client.py:347-359` выставляет
  `last_result_truncated`, но **ни один вызывающий его не читает** (единственный
  потребитель — `odata_client.py:907`). Срез по `max_pages=1000` даст массовое ложное
  закрытие.

Фон раз в час (`sync_orchestrator.py:158`, `dry_run=False`). §12.6: «неполная,
обрезанная или **отфильтрованная** выгрузка отсутствия не доказывает». Закреплено
`test_production_order_sync.py:111-165`.

Тот же слепой к обрезке шаблон: `odata_stock_sync.py:386-431` (обнуление `stock_qty`).

Смягчает: запись только локальная, в 1С ничего не уходит.

### C-3. Пересчёт плана удаляет предложения без read-back
`period_plan_service.py:1300-1327`: сохраняет `PlannedPurchase` с локальным
`SyncLink(success)`, остальные удаляет. Доказательство — локальный sync_link, read-back
в 1С не выполняется. В окне «POST прошёл, коммит sync_link упал» предложение удаляется,
а заказ в 1С остаётся.

---

## 4. P1 — НЕРЕАЛИЗОВАННЫЕ РАЗДЕЛЫ КАНОНА

| Раздел | Состояние | Где |
|---|---|---|
| §14 агрегированная строка закупки | **0 из 5 пунктов**. Чистый FIFO по всем buy-резервам. `PurchaseExportObligationAllocation` в модуле атрибуции не читается **ни разу**; единственный читатель во всём backend — `supplier_future_supply.py:167-176` (ожидаемая поставка). Таблица заполняется и не используется | `supplier_receipt_allocation.py:311-336` |
| §15.2 возврат по строке заказа | Отсутствует — идёт сразу глобально newest-first. Структура `active_by_order` создаётся (`:306`), заполняется (`:325-328`), **не читается нигде** | `supplier_receipt_allocation.py:341-345` |
| §18 пустой/неизвестный маршрут | «everything else ⇒ production». Распознавание подстрокой (`покуп`/`закуп`/`buy`); всё прочее → `make`. Fail-closed нет нигде. Позиция с незаполненным `СпособПополнения` фиксируется производственной — неисправимо после заморозки | `replenishment.py:19-33`, `reservation_ledger.py:142-150` |
| §8 несколько строк одного изделия | При `len(line_ids) > 1` внутри связанного плана → `status="ambiguous"` → **весь факт в surplus**. Канон явно: oldest-first внутри плана, «не превращает принятый факт в surplus». Плитка не закрывается никогда. Закреплено `test_ambiguous_exact_never_allocates` | `assembly_output_persistence.py:242-246`, `assembly_output_core.py:83-91` |
| §10 полка/вытягивание для BOM-уровня ≥ 2 | Join `MrpFreezeComponent.parent_item_id == DrumSlot.item_id`, а `DrumSlot.item_id` — всегда ГП. Кумулятивной нормы «на единицу изделия» нигде нет. Для детали под подсборкой `component_demand = 0` → `pull_qty = 0` → журнал молча «буфер полки закрыт», строку **невозможно запустить**. Тесты покрывают только уровень 1 | `shelf_projection_persistence.py:62-72` |
| §10 приоритет мехцеха (5 уровней) | Не реализован: уровни 2-5 не вычисляются, единого списка мастеру нет. Расход `shelf_allowance` идёт в порядке `work_item_ids` от оператора | `shelf_projection_core.py:151-155` |
| §4 `covered_from_stock_at_freeze_qty` | Включает открытый WIP: `net` = после stock **и** `LedgerFutureSupply(wip_order)`. Поле «покрыто со склада» хранит `наличие + WIP`. Замороженная `replenishment_required_qty` занижена; отмена заказа дефицит не восстановит | `period_plan_service.py:845-890` → `reservation_ledger.py:197-201` |
| Отмена → повторная материализация с lineage | Пути нет. `cancel_local_order` ставит `deletion_mark`, но `_active_mrp_products_for_requirement` отменённые не исключает → повтор возвращает отменённую строку как `reused`. У `buy` успешный claim блокирует резерв навсегда, ручки отмены нет | `production_control_journal.py:189-212,1258-1341` |

---

## 5. P1 — ВТОРЫЕ ДВИЖКИ И ВТОРЫЕ ХРАНИЛИЩА

### Д-1. `items.stock_qty` — второе живое хранилище остатка
Модель `models.py:455`; писатель `odata_stock_sync.py:236,433` в фоновой работе
(`sync_orchestrator.py:124-126`); читается в `schemas.py:15,63` (все `/items*`),
`routers/specification.py:156` (дерево BOM — публичный GET **без метаданных истины**),
`priority_manager.py:48,73`.

`item_ledger/reconcile.py:13-16` в докстринге **легитимизирует** его как «stock-sum
compatibility projection» — прямо против `CANON.md:124,188`.

Экран спецификаций показывает остаток другого происхождения и другой даты, чем все
ledger-проекции.

### Д-2. Три построителя очереди сборки + запрещённый кросс-валидатор

| | источник периода | доп. фильтр | нетто по выпуску |
|---|---|---|---|
| `assembly_queue_snapshot.py:53` | `run.period_from/to` | — | да |
| `drum_schedule_persistence.py:38` | `run.period_from` **or** `plan.period_from` | — | да |
| `assembly_output_persistence.py:133` | `plan.period_from/to` | `bucket_date >= period_from`, `fixed_at not null` | **нет** (берёт `line.qty`) |

Плюс `_assert_queue_matches` (`drum_schedule_persistence.py:120`) **пересчитывает**
очередь и сверяет с сохранённой — буквально запрещённый каноном кросс-валидатор.

Расхождение фолбэков: снимок при `period_from is None` подставляет `""` в sort_key и
ставит строку **первой** в очереди.

Строка с `bucket_date < period_from` видна очереди и барабану, но невидима аллокатору
факта → её плитка неубиваема.

### Д-3. Два fold резервов с разным фильтром поколения
`reservation.py:88` (`fold_reservation_entry`, **без** фильтра
`ledger_generation_id`) и `reservation_ledger.py:243` (`_fold_entry`, **с** фильтром).
Обе живые: первая — из `historical_replay_persistence.py:384` и
`supplier_receipt_allocation.py:610`; вторая — из `reservation_ledger.py:333` и
`historical_obligations.py:293`. При пересечении событий поколений дадут разное выполнение.

Третья (мёртвая) реализация — `generation_reconciliation.py:36` + явный кросс-валидатор
`:110-113`, прод-вызовов нет, в реестре не значится.

### Д-4. Канонический владелец формулы заморозки мёртв
`reservation.py:34-45` (`freeze_reservation_amounts`) — вызовов нет, только реэкспорт
и тесты. Живая заморозка считает по другому выражению из других входов:
`reservation_ledger.py:197-201` (`total_required − net_required`). «Единственная
формула» существует только на бумаге.

### Д-5. Третий аллокатор фактов вне реестра
`production_fact_projection.py:145-329` — собственный «точная связь → FIFO» над теми же
`assembly_in`, вызывается из `production_order_sync.py:656,682`. При переполнении
сознательно доливает излишек на последнюю строку заказа сверх её `quantity`
(`:325-328`), а кламп в `production_output_truth` это скрывает.

### Д-6. Два вычислителя эффективного остатка с расходящейся политикой
`mrp_freeze.py:184-227,393-404` фильтрует по `StockBin.organization_ref`;
`mrp_stock_helpers.py:57-93` — **нет**. Плюс `routers/item_ledger.py:209-238` и
`reservation_ledger.py:96-138` — итого четыре копии формулы «контур × сумма StockBin».
Канонический `reconcile.contour_warehouse_refs` имеет одного вызывающего.

Докстринг `mrp_stock_helpers.py:1-14` признаёт дубль и ссылается на функцию, которой
уже не существует.

### Д-7. Мёртвый параллельный планировщик (~500 строк)
`planning_service.py:2009-2597` (`build_planned_orders_and_purchases`,
`build_order_stages`, `apply_capacity_constraints`) + `order_quantity_calculator.py`,
`capacity_scheduler.py`, `priority_manager.py`. Прод-вызовов нет, живы тестами.
Внутри: `lead_time = item.replenishment_time or 30` (`:2214`) — валидный ноль §17
превращается в 30 дней; `date.today()`; чтение `items.stock_qty`.

`CapacityScheduler` при этом **живой** (`period_plan_service.py:1672`): свой календарь
Пн-Пт игнорируя `WorkCalendarDay`, своя формула мощности, `date.today()` — не привязан
к поколению, невоспроизводим. Его даты используются как **легаси-фолбэк**:
`production_control_journal.py:855-862`, комментарий прямо гласит «the legacy
CapacityScheduler dates stay as fallback».

### Д-8. Четыре писателя в 1С вне реестра `one_c_*_export.py`
- `production_control_material_issues.py:1474-1499` — PATCH + Unpost/Post (кнопка «Собрано»);
- `spec_writeback_1c.py:164-170` — PATCH `Catalog_Спецификации` (см. Б-7);
- `purchase_control_materialization.py:1129-1234` — второй путь создания
  `ЗаказПоставщику` со своей нумерацией `PC-{supplier}-{hash}` и **мёртвой** проверкой
  дублей: `top=1, max_pages=1`, затем `if len(recovered) > 1` (недостижимо), далее
  молча `recovered[0]`. Канонический аналог `one_c_export_common.py:100-115` сделан верно;
- `one_c_piecework_export.py:893,1140` — через `getattr(client, "patch")`.

### Д-9. Живой статус перемещения подмешан в неизменяемый снимок
`production_control_journal.py:173-186` (`_journal_coverage_status`): `coverage_status`
считается из операционных `state.issue_status`/`state.status`
(`posted → assembled`, `requested|issued|exported → to_move`) и **перекрывает**
generation-bound `material_coverage_status`. Результат вмораживается в снимок (`:1109`)
и фильтруется публичным GET (`production_control_journal_snapshot.py:786-789`).

Прямо против `CANON.md:67` и planning-truth-contract. Подтверждено двумя аудитами.

### Д-10. Живой путь custody включён по умолчанию
`production_control_material_availability.py:248` — `use_custody_projection: bool = False`.
В проде единственный вызов идёт с `True`, но дефолт — живой `load_material_custody` +
живые статусы выдачи (`:333-344`). «Легаси за флагом» без даты сноса
(правило расширения 6). Живой путь также в командных операциях
(`production_control_material_issues.py:961`, `production_control_production_flow.py:108`,
`paint_weld_pairs.py:397`).

### Д-11. Живой пересбор маршрутного листа сохранён
`production_control_printing.py:1095-1117` (`render_route_sheets_html`) — роутеры его
не зовут (публичный путь чист и полностью канонический), но функция жива и покрыта
тестами. Готовый fallback без даты сноса.

### Д-12. Копии витринной математики
`_coverage_percent` побайтово одинаков в `purchase_control_snapshot.py:160` и
`purchase_control_journal.py:22`. Inline-варианты: `period_plan_service.py:2546,2554,
2566,2576,2585,2595,3124-3126` (`round(..., 1)` против 6 знаков у владельца и `Decimal`
в `reservation.py`), `planning_service.py:1536`. Владелец
`replenishment_execution_pct` импортирован в `period_plan_service.py:56` и не вызван.

### Д-13. Живой путь `action="refresh"` (повторный MRP фиксированного плана)
`mrp_freeze.py:828,860-878` + `planning_run_candidate.py:186-239`. Производителей
`refresh` сегодня нет (`obligation_refresh_manifest.py:323-345` выдаёт только
`retain`/`retire`/`add`), но путь полностью собран и покрыт тестами: заново разворачивает
BOM и переписывает `reserved_qty`/`covered`/`replenishment_required`. Один вызов из
воркера воспроизведёт запрещённый повторный MRP.

---

## 6. КАЧЕСТВО МЕХАНИЧЕСКИХ ПРОВЕРОК

### Восемь архитектурных инвариантов `CANON.md:184-192`

| # | Инвариант | Покрыт | Надёжность |
|---|---|---|---|
| 1 | фикс. список писателей `ReservationEvent` | **ДА** | **высокая** — AST по всему `backend/app`, точное равенство множества. Фактически ровно два писателя, совпадает |
| 2 | единственные точки записи в 1С | ДА, частично | **средняя** — сканируется только `services/` (не `routers/`, `scripts/`, `sync_worker.py`, `tools/`); ловит только `client.post/patch/delete` буквально; **`post_operation` не в списке**; `put` не проверяется. Пропускает 4 реальных писателя (Д-8) |
| 3 | нет второго хранилища остатков | **НЕТ** | проверки не существует; инвариант **фактически нарушен** (Д-1) |
| 4 | нет второго вычислителя reserve/replenishment/execution | ДА, слабо | **низкая** — проверяет, что *имена* определены в `reservation.py`, но не что они **вызываются** (мёртвый владелец Д-4 проходит). Эвристика AST `BinOp(Sub)` требует «required» И одно из received/fulfilled/realized — пропускает `total_required − net_required`, `progress_base − completed`, `planned − accepted`, весь Д-3 и Д-12 |
| 5 | нет ручных DBR-программ и `program_id` | ДА, частично | **средняя** — надёжно проверяет отсутствие `routers/dbr.py`, пустоту `services/dbr/`, 9 legacy-моделей. **`program_id` не проверяет вообще**. Фактически чисто |
| 6 | нет предметной математики и прямого HTTP во frontend UI | HTTP — **ДА**; математика — **НЕТ** | HTTP: `uiServiceBoundary.test.ts` — честный TS-AST, allowlist в этой волне **сократился**. Дыры: только `src/ui` (не `domain`/`lib`), не ловит `XMLHttpRequest`/`sendBeacon`/`EventSource`. **Математика не проверяется ничем** — ключевой запрет канона без гейта |
| 7 | один `planning_run` на фиксированный план | НЕТ (в канон-гейте) | **средняя** — защищён partial unique индексом в БД (`models.py:1399-1412`) + `_lock_mrp_ledger`; в канон-гейте инварианта нет, на SQLite не эквивалентно PG |
| 8 | нет ссылок на несуществующую документацию | **НЕТ по существу** | **низкая** — black-list из 10 захардкоженных имён, а не проверка целостности. Висячие ссылки существуют, гейт зелёный |

**Итого надёжно покрыто: 2 из 8.**

### Молчаливые пропуски

В самом `test_canon_invariants.py` — **ни одного**: нет `skip`, `xfail`, `try/except`,
`_read_owner_document` падает жёстко. Исправление инцидента держится.

**Но в общем прогоне пропуски есть.** `tests/test_material_issue_locking.py:40`,
`tests/test_pg_rebuild_check.py:104`,
`tests/services/test_reservation_replenishment_core_migration.py:30` — `skipif` по
доступности PostgreSQL. В `.github/workflows/canon.yml` **сервиса PostgreSQL нет**,
поэтому в CI все три молча пропускаются: не проверяются ни семантика миграций резервов
на PG, ни advisory-lock от двойного расхода, ни rebuild-верификатор.

Это ровно паттерн «тест, который пропускает отсутствующую истину без шума» —
корневая причина №4 инцидента.

### Frontend-гейты (`frontend-framework.md:269-277` требует семь)

| Запрет | Гейт |
|---|---|
| прямой `fetch`/`api` из `ui` | **работает** |
| новые `any` | работает, но **неявно** — правило из пресета `tseslint`, исчезнет при мажорном апгрейде без правки в репо; `eslint .` без `--max-warnings 0` |
| ручное дублирование DTO | **нет** |
| открытые enum `Known \| string` | **нет** (3 нарушения живут) |
| предметная математика в `ui`/`domain` | **нет** |
| третья копия таблицы/диалога/picker | **нет** (4 диалога, ~14 таблиц, 3 CSV-механики) |
| Playwright smoke | **отсутствует в CI** — `canon.yml` гоняет типы/lint/test/build; 12 visual-baseline изменены в этой волне и не проверены |

Новый `test_frontend_openapi_routes.py` полезен (регресс `stage-distribution` поймал бы),
но частично декоративен: сканирует только строковые литералы — пути вида
`` `${BASE}/restage` `` не проверяются вообще; `startswith(candidate + "/")` пропускает
любой обрезанный префикс; о **типах** ответа не говорит ничего.

### Абсолютные машинные пути — прямое нарушение `CANON.md:12`

- `.docs/prodplan-shadow-deploy.md:58-59` — `/home/ivan/PRODPLAN/...` в **живой** документации;
- `tools/inc0_odata_movements_probe.py:19-21` — три пути в инструкции запуска.

Допустимо (историческая память): `artifacts/canon-reconciliation-2026-07-30/`,
`.docs/notes/incident-2026-07-canon-context-loss.md:24`.

**В проверках истины путей нет** — `_read_owner_document` читает `REPO/.docs/notes/`.

### Мёртвые указатели на документацию

- `production_order_sync.py:333` → `.docs/production_orders_odata_queries.md` (нет файла) — **живой прод-код**;
- `.docs/notes/codex-handoff-2026-07-26-produce-restore.md:24` → удалённая миграция;
- `docs/archive/reports/order_zsnf-000943_report.md` → тот же несуществующий файл.

Агент идёт по мёртвому указателю → чинит по догадке из кода. Точный механизм инцидента.

### Устаревшая живая документация

`frontend-erp-shell/docs/FRONTEND-TECHDEBT.md` описывает как реализованные и покрытые
baseline экраны **DBR Feeder, DBR Drum Board, DBR Settings, DBR Programs, DBR Purchase** —
все удалены, в `src/ui` нет ни одного `Dbr*`. Плюс пункт «P2 — типизация границы API
(`any`)» противоречит шапке того же файла и факту (ноль `any` в проде).

---

## 7. ПОДТВЕРЖДЕНО СООТВЕТСТВУЮЩИМ КАНОНУ

Существенная часть ядра держится строго. Ниже — то, что проверено и подтверждено.

**Публикация и чтение**
- Атомарность: оба публикатора строят кандидат и переключают указатель в ОДНОЙ
  транзакции без внутреннего commit; compare-and-set указателя под row-lock
  (`planning_truth.publish_generation:278-299`).
- Идемпотентность точного повтора — no-op на всех уровнях; перезапись другим
  содержимым → `PlanningSnapshotConflict`.
- **Проверены все 78 `@router.get`**: ни один публичный GET не делает
  `commit/flush/add/merge/delete` и не вызывает `build_*/publish_*/promote_*/materialize_*`.
  `get_db` — `autocommit=False, autoflush=False`.
- Пять структурных чекпойнтов реально держат инварианты 1-3 (fold резервов,
  сохранение факта по каждому SLE, fold бинов, `0 ≤ net ≤ gross`, work-items).
- Маршрутный лист — **полное** соответствие: GET и POST печати читают только
  сохранённый payload, легаси-параметр `mark_printed` в GET обезврежен,
  отсутствие payload → `unavailable` без live-фолбэка.

**Фиксация плана**
- Однократность и атомарность фиксации; повторная фиксация идемпотентна и не форкует
  поколение.
- Один `planning_run` на план гарантирован partial unique индексом.
- `refreeze_active_snapshots` изъят и бросает исключение.
- Три замороженные величины переносятся в новое поколение побайтно, без пересчёта.
- Пул строится ОДИН раз; `pool_key_for` — единственный производитель ключа.
- **§13 — обе половины**: минус принимается как 0 в пуле распределения и удерживается
  так; физический минус сохраняется в базисе и подсвечивается
  `NEGATIVE_PHYSICAL_STOCK_UNAVAILABLE` вплоть до опубликованного снимка.
- **§17 синхронизация**: `СрокПополнения = 0` принимается и сохраняется (проверка
  `is not None`, не truthiness).
- Выбор спецификации fail closed; `component_spec_ref1c` всегда сильнее default.
- Политика клампов соблюдена; диагностический `available` может быть отрицательным.

**Атрибуция**
- Формулы §5 дословно; процент при нулевой базе — `None`, не 0 и не 100.
- Резерв не уменьшается при поступлении (`reserved_delta = 0`).
- **Направления сортировок не перепутаны**: размотка newest-first, назначение oldest-first.
- §15.1 и §15.5 корректны; повторная размотка и уход ниже нуля закрыты тремя уровнями.
- Один факт назначается не более одного раза; `allocated + surplus == fact_qty`.
- Изоляция поколений при refresh: перенос с `realized_qty = 0`.

**Граница с 1С**
- **§12.3 соблюдён полностью**: точек записи `СостояниеЗаказа_Key`/`ВариантЗавершения`
  не существует ни одной; все вхождения — чтение. Проверены и побочные пути.
- Контур «Произвести» цел; факт не объявляется, `ledger_readback: queued`.
- `PATCH {"Posted": true}` отсутствует полностью; проведение только
  `Unpost` + `Post?PostingModeOperational=true`.
- Приходные/оприходования/списания не создаются; `dry_run` — настоящий предпросмотр
  во всех экспортёрах (клиент создаётся строго после ветки return).
- Массовой фоновой write-выгрузки нет; резервы не экспортируются.
- `ЕдиницаИзмерения` GUID+тип везде, кроме одного fallback
  (`one_c_purchase_order_export.py:482-486`).

**Барабан и полки**
- `assembly_remaining_qty` формула верна; `planned_output_qty` неизменяемо.
- Каталог `services/dbr` отсутствует; `program_id` — только в миграциях сноса.
- Детерминизм и идемпотентность барабана с контролем сохранения количества
  (`row_slot + row_gap == row_open`, иначе ValueError).
- `protection_days` не подменяет MRP-срок; `Item.replenishment_time` в контуре полок
  не используется ни разу.
- Один факт — две проекции без двойного учёта между очередью и выполнением резервов.
- `order_quantity_calculator.py:76` — `buffer_qty` жёстко занулён с комментарием
  «buffer must NOT translate into additional quantities».

**Журналы**
- Журналы не имеют собственной формулы потребности; одна потребность — один журнал.
- Публичного API правки количества или дедупликации MRP-строк **нет** (проверено по
  всему репозиторию; обход только косвенный — C-1).
- `production_control_reservations.py` и `recalc_production_coverage.py` удалены **без
  остатков**; у `ProductionOrderLineState` нет ни одной coverage-колонки.
- `remaining_qty` нигде не читается как факт — все чтения через
  `production_output_truth`.
- Fail-closed на непринятом снимке: все публичные чтения дают 503 с `truth_status`,
  `ledger_generation`, `cutoff`, `reason`.

**Удаления волны — чисто**
- Сверено с `git show HEAD:<путь>`: ни один публичный символ удалённых модулей не
  воскрес под другим именем (0 вхождений `load_reservation_state`,
  `create_forced_order_request`, `calculate_stages`, `calculate_resource_distribution`,
  `run_planning_run`, `export_shortage_report_for_run` и ещё 10).
- Дропнутые таблицы (`forced_orders`, `production_plan_entries`,
  `material_coverage_cache`, `root_products`): остаточных ORM- и raw-SQL-ссылок нет.
- `stage-distribution` вычищен на всех слоях фронта, включая smoke-baseline.
- Удалённые тесты обоснованы — сняты вместе со своими целями.

**Frontend**
- HTTP только в `src/services` — ноль протечек в проде.
- Ноль `any` в проде.
- **Параллельных MRP/DBR-страниц нет** — «очередь мехцеха» правильно сделана режимом
  существующего журнала.
- Открытие страницы тяжёлый расчёт не запускает; latest-wins защита реализована.
- `ui/item-ledger/*` — образцовое соответствие «главной границе».

---

## 8. НЕПРОВЕРЕННОЕ

1. Полный `pytest` волны не прогонялся (аудит только на чтение). Канон требует цифру
   с ФИНАЛЬНОГО коммита волны — она не снята.
2. Поведение миграций `20260731_01..07` на реальном PostgreSQL — PG-тесты требуют
   `PRODPLAN_TEST_PG_URL`, которого нет ни локально, ни в CI.
3. `npm run test/lint/build/smoke` не запускались.
4. Численное соотношение `replay_from` и якоря ингеста (риск двойного зачёта между
   `covered_from_stock_at_freeze_qty` и выполнением) — нужны данные боевой БД.
5. Расхождение трёх построителей очереди на реальных данных — установлено по коду,
   численно не измерено.
6. 12 изменённых visual-baseline не сверены.
