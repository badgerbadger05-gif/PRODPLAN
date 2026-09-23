# Контракт источника истины

## Обязательство и факт

Зафиксированный план и его MRP-снимок являются обязательством. Принятое
поколение Item Ledger является единственным источником физического факта.

Обязательство после фиксации не переписывается фактами.

## Что считается фактом

Только принятые записи Item Ledger подтверждают:

- остаток и физическое движение;
- выпуск, поступление, возврат и списание;
- покрытие потребности пополнения;
- выпуск верхнеуровневой строки плана.

Заказ, его статус, дата, `produced_qty`, `received_qty`, `remaining_qty` и
прочие накопительные зеркала являются операционными сведениями и
происхождением. Они не являются источником выполнения.

### Идентичность и границы публикации

`generation_id`/`ledger_generation_id`, `parent_generation_id` в sealed
watermark JSON и `snapshot_id` описывают принятую границу, provenance и
идемпотентность read-model. Они не являются устойчивыми ключами движения,
плана, MRP, резерва или заказа. Полные предметные ключи и inventory
читателей/писателей зафиксированы в
[`docs/r1-data-contract-inventory.md`](../docs/r1-data-contract-inventory.md).
Поколение фактов не переанкеривает frozen-обязательство; sealed lineage
обходится только каноническим `live_plan_scope.py`.

Данные делятся на факты, frozen-обязательства, текущие производные итоги,
основания назначений, технические метрики и историю бизнес-команд. Текущий
итог записывает только владелец основания; второй сервис, GET, frontend или
статус заказа не может переписать его отдельно.

Каждый принятый факт и correction сохраняет `posting_at` (время события в
источнике) и `known_at` (время принятия в PRODPLAN). Исторический ответ обязан
явно выбрать `as_occurred` или `as_known` и сообщить обе даты; отсутствие
режима не разрешает молчаливый выбор другой временной оси.

## Назначение факта

Для выполнения пополнения точная канонически подтверждённая связь с живым
резервом имеет приоритет: факт сначала назначается этому резерву в пределах
его незакрытого остатка. Излишек распределяется по старейшим остальным
активным потребностям номенклатуры FIFO.

Факт без точной связи, с неизвестной или неоднозначной связью, по неизвестному
PRODPLAN заказу 1С либо по уже закрытому резерву полностью распределяется
FIFO. Provenance не может сделать принятое физическое количество
неучитываемым. Один факт и его излишек назначаются количественно только один
раз.

Агрегированная строка закупки использует все сохранённые экспортные
назначения: связанные живые резервы oldest-first, каждый в пределах
`allocated_qty`, затем общий FIFO. Отрицательный факт разматывает назначения:
по ссылке на исходное поступление — точно; по строке заказа — newest-first
внутри строки, затем глобально; без ссылки — глобально newest-first. Уже
отменённое количество повторно не разматывается.

Для базиса нового плана принятый остаток экранируется динамическими
удержаниями старших живых резервов. Удержание уменьшает только назначенный
физический расход: точно связанный сначала адресно, без связи FIFO. Поступление
удержание не освобождает. Небезопасная атрибуция даёт `unavailable`.

Для закрытия очереди сборки точная подтверждённая связь выпуска с планом
закрывает строку этого плана. Если такой связи нет, выпуск распределяется по
старейшим открытым строкам сборки этого изделия FIFO.

Неоднозначность сохраняется в provenance. Факт не остаётся количественно
`unplanned` и не приписывается произвольно.

## Поколение

Расчёт строится как согласованный кандидат одного `ledger_generation` и
`cutoff`, проверяется и публикуется атомарно.

Один опубликованный снимок должен согласованно содержать:

- физическую ленту и остатки;
- живые резервы;
- адресно-FIFO покрытие потребностей;
- остатки выпуска планов;
- очередь сборки и раскладку барабана;
- проекции производственного и закупочного журналов;
- обеспеченность материалами каждой строки производственного журнала.

Обеспеченность материалами строится только из данных того же поколения:
остатков и резервов Item Ledger, generation-bound будущих поставок и
generation-bound проекции custody. Живые статусы перемещений, заказов,
плановых заказов и ETA запрещено подмешивать в candidate. Если custody нельзя
воспроизвести на cutoff из append-only событий, candidate не публикуется.

Поколение фактов и поколение обязательств расходятся по времени. Фактовый форк
публикует физику и не переанкоривает обязательство: зафиксированный run и строки
его обязательства остаются на том поколении, которое их заморозило, вместе со
своим `cutoff`. Поэтому «жив ли run для принятого поколения» решается только
sealed-цепочкой `parent_generation_id` — привязка внутри цепочки плюс
равенство cutoff run и cutoff его поколения-анкера. Сравнение привязки или
cutoff обязательства с текущим принятым поколением означает, что читатель
замолчит после первого же фактового форка.

Read-model, проецирующий только зафиксированное обязательство, публикуется один
раз на поколение обязательства и читается по той же sealed-цепочке; фактовый
форк его не перестраивает и не дублирует. Такой снимок сообщает поколение и
cutoff своей публикации и не выдаёт себя за текущее. Read-model, проецирующий
факты, остаётся привязанным к одному принятому поколению и одному cutoff.

Маршрутный лист является частью неизменяемого read-model производственного
журнала. BOM, операции, перемещения и связь «окраска-сварка» разрешено читать
из рабочих таблиц только при сборке candidate. GET и POST печати рендерят
сохранённый payload принятого поколения; отсутствие payload у старого снимка
означает `unavailable` до следующей полной публикации, без live fallback.

## Fail closed

Пустой, неполный, непринятый, отозванный или stale Ledger означает
`unavailable`.

Порог свежести принятого поколения (§40) применяется ко всем, кроме ровно
двух точек входа: публикации физического refresh
(`publish_forward_physical_refresh_current`) и no-op восстановления текущих
областей от указателя (`repair_current_execution_scopes_from_pointer`). Они —
механизм, который делает принятое поколение свежим; проверка их собственных
расчётов по возрасту того же указателя даёт взаимную блокировку: после простоя
дольше порога ни один тик не может завершиться, и снять это состояние нечем.
Внутри них снимается только проверка возраста. Согласованность с точным
указателем, требуемые capabilities и операторская инвалидация действуют
по-прежнему. Обязательственный refresh и приёмка поколения исключением не
являются: обязательственный refresh наследует cutoff родителя и свежесть не
восстанавливает, а фиксирует MRP, поэтому на указателе старше порога он
отклоняется с причиной stale, как любая фиксация MRP.

В этом состоянии запрещено:

- показывать ноль вместо неизвестного факта;
- выполнять MRP, FIFO, барабан или материализацию;
- считать неизвестный способ пополнения производством по умолчанию;
- переключаться на legacy-агрегаты;
- публиковать частично согласованный снимок.

## Чтение

GET, открытие страницы, фильтрация и экспорт читают сохранённый снимок.
Тяжёлый расчёт выполняется worker-ом или явной командой и никогда не
запускается скрыто из UI.

Снимок содержит не только исходные строки, но и все необходимые странице
итоги, остатки, проценты, статусы, приоритеты, разрешённые действия и
группировки. Frontend не достраивает расчётную проекцию из сырых строк.

## Инварианты

1. Плановое обязательство неизменяемо.
2. Один физический факт учитывается один раз.
3. Сумма назначений не превышает факт.
4. Один cutoff используется всеми связанными проекциями.
5. Повторная обработка одного поколения идемпотентна.
6. Legacy fallback отсутствует.
7. Любой DTO факта передаёт generation, cutoff и truth status.
8. Идентичность основания сохраняется при техническом fork; повторная
   публикация не копирует неизменённые пары fact/receiver.
9. Количества назначений используют точный `Decimal(15,3)` контракт; округление
   и замена неизвестного значения нулём запрещены.

## R6. Компактная физика и custody

Текущий `StockBin` — одна строка на полный physical key
`(item, characteristic, organization, warehouse)`. `ledger_generation_id`
обязателен только как provenance принятого Ledger; `is_current` отделяет
подготовленный BUILDING candidate от принятого результата. Candidate не виден
другой сессии до acceptance commit, а superseded current rows удаляются при
публикации. Отрицательный on-hand сохраняется; свободный S0 не может получить
его как положительный ресурс.

Custody остаётся event-sourced: события неизменяемы, baseline задаёт
минимальную доказанную точку rewind, а compact current projection публикуется
отдельно после успешной проверки candidate. Позднее событие за всеми
восстановимыми baseline даёт `unavailable`; удаление baseline или projection
допустимо только если остаётся доказуемый путь воспроизведения. Отсутствующий
или рассогласованный current marker не заменяется старым поколением.
Локальные `issue_created`/`terminal_release` writers сначала блокируют
единственный `PlanningTruthState` marker, затем получают event id и обновляют
projection; перескок через unseen physical/backdated event запрещён.

## R7. Выпуск плана, rebase и future supply

Накопленный выпуск строки плана читается из сохранённых
`accepted_output_qty/remaining_output_qty`; stable execution fact
`(stock_ledger_entry_id, plan_line_id)` защищает повторный read-back. Раскладка
использует общий `document_net_output.py` и один exact-then-FIFO allocator.
`AssemblyOutputAllocation` generation-scoped только как audit/provenance и не
является вторым владельцем количества.

Future supply разделяет bounded BUILDING staging и compact current read.
`LedgerFutureSupply` существует только до успешной публикации accepted
generation и затем удаляется в той же транзакции; `LedgerFutureSupplyCurrent` —
единственный compact current quantity owner по
`current_identity`; `LedgerFutureSupplyCurrentChange` хранит before/after audit.
Legacy `is_current` не является runtime-reader fallback. Accepted/current readers
используют только exact pointer, BUILDING readers — staging через единый helper;
latest/max heuristic и fallback при отсутствующей truth запрещены. Compact
publication и accepted truth pointer переключаются атомарно; ambiguous/rejected
capture current reader не видит. Change audit растёт только при semantic change.

Specification import сначала приводит payload к семантическому canonical form
(включая Decimal и порядок строк), затем сравнивает единственный revision hash.
Equivalent import не создаёт successor; genuine change закрывает старый MRP и
создаёт ровно один successor на сохранённый remaining basis. Read-model не
пересчитывает накопленный output и не выводит execution из статуса заказа.

## Транзакционное текущее пополнение (R4)

Текущая supplier-receipt replenishment assignment является одной
транзакционной проекцией: assignments, `ReservationEntry` execution fields и
compact source/revision marker либо видны вместе после commit, либо не видны
вовсе. Писатель — `current_replenishment.py`; generation и snapshot не
создаются и не являются identity текущей пары.

Scope marker идентифицируется полным ключом `(item, characteristic,
organization, planning_stock_pool, mode)`. `source_key` и `source_revision`
хранятся отдельно и не могут обойти stale/drift check. `complete_scope=True`
обязателен; пустой, но подтверждённый scope обязан передаваться явно и может
очистить только собственный scope. Неопределённый или частичный вход
fail-closed.

`ReservationConsumptionAllocation` с `is_current` хранит стабильное основание
`(sle_id, reservation_id)` независимо от generation provenance. Audit хранит
только изменённые пары. Исторический `ReservationEvent` supplier writer не
является параллельным current owner и после принятого R4 marker блокируется
для того же scope; assembly_out consumption не смешивается с supplier receipt.
В таблице явно хранится `allocation_role`: `material_consumption` используется
freeze/custody consumers, `replenishment_receipt` — только current
replenishment reader. Пересечение ролей в quantity sums запрещено.

### Stable reservation owner

`ReservationEntry` сохраняет один stable owner на business identity
`reservation:req:<requirement_id>:mode:<realization_mode>`. Generation — только
provenance: accepted/current readers читают `is_current` для exact
`PlanningTruthState` pointer, а `owner_kind=building` разрешён лишь в bounded
staging до атомарной публикации. Physical refresh не создаёт persistent
reservation copy; publisher set-based rebind-ит allocations, work items и
purchase dependencies, затем удаляет staging duplicates. Failed build не
меняет prior current owner. Migration `20260914_01` не выполняет этот
historical rebind: она bootstrap-ит только exact accepted pointer, оставляя
старые rows с `owner_kind=legacy`/`is_current=false`. Их compaction и archive
перенесены в отдельную будущую GC revision после cutover projections и
проверки bounded dependency/backup safety.

`ReservationEvent` получает semantic `event_identity` без generation/cycle и
явный `origin_kind` по событийному смыслу. Дубликаты одного physical fact при
разных cycle/idempotency схлопываются, фактические/correction events сохраняются
ровно один раз. `ReservationCurrentChange` — append-only audit только
semantic changes; технический replay не является audit. Legacy raw events не
используются как runtime fallback: восстановление обеспечивается verified
backup. В `20260914_01` archive только создаётся пустым; compact semantic
archive и удаление legacy copies выполняются отдельной будущей GC revision.

R5 использует этот же writer для полного signed replay supplier receipts,
corrections и returns. Current хранит только положительный итог basis; exact и
FIFO части одного стабильного pair получают `mixed`, а over-return остаётся
явным unmatched result без отрицательной allocation. `posting_at` и `known_at`
сохраняются вместе, `history_mode` выбирается явно, closed reservation не
переоткрывается и assignment не переносится на новую obligation только по
совпадению item.

## R8. Текущая область исполнения

Очередь assembly, readiness, drum и shelf имеют единый compact current owner:
`CurrentExecutionRow`; `CurrentExecutionScope` — единственный manifest и
semantic pointer, включая валидный пустой scope, source revision/generation,
content hash и сохранённое summary. Публичный GET читает только опубликованный
manifest/current rows. Missing, stale, not-ready или mismatched manifest
fail-closed; generation-scoped rows и legacy read-snapshot таблицы не являются
runtime fallback.

Generation-local staging ids не входят в business payload: readiness и drum
ссылаются на stable current queue owner. Новая accepted technical generation
с тем же результатом меняет только manifest provenance, без current row,
updated_at или audit churn. Current drum order использует typed deterministic
date/resource/priority/ordinal tie-break.

Переходный compatibility owner для manual drum actions, work-item navigation и
supplier provenance ограничен exact accepted generation. После атомарной
публикации current execution удаляются только retired non-building projection
copies set-based и в FK-safe порядке; активный BUILDING staging и exact current
generation сохраняются. Это bounded retention, а не удаление ledger/history:
полный GC разрешён только отдельной волной после cutover зависимостей и
backup prerequisite.
Миграция cutover дополнительно требует PostgreSQL session guard
`prodplan.execution_projection_backup_ready=on`; отсутствие guard блокирует
любой DELETE.

Reference, custody и manual mutation writers инвалидируют только свои
зависимые manifests и делают это на фактическом semantic change; no-op update
не инвалидирует готовый результат. Текущего Calendar writer/API нет. Любой
будущий writer обязан вызвать
`invalidate_current_execution_for_calendar_change` в той же транзакции;
out-of-band изменение без hook оставляет manifest ready и не обнаруживается
автоматически — это явный unsafe gap до появления writer.
