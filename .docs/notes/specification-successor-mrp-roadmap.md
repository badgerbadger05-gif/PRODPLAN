# Roadmap: successor-MRP при изменении спецификации

## Цель

Изменение спецификации не переписывает уже замороженный MRP. Старый
MRP закрывается с фактическим исполнением, а successor-MRP создаётся только на
остаток недовыпущенных корней.

```text
Исходный план: 10
MRP-1: план 10, принятый выпуск 8, статус CLOSED/REBASED
MRP-2: план 2, новая BOM-ревизия, новые резервы и заказы
Lineage total: план 10, выпущено 8, осталось 2
```

## Неизменяемые правила

1. Остаток корня: `max(plan_line.qty - accepted_output_qty, 0)`.
2. `accepted_output_qty` берётся только из принятого Item Ledger.
3. Старый BOM, резервы, заказы и цифры исполнения не изменяются.
4. Неисполненные резервы старого MRP снимаются целиком.
5. Successor-MRP заново раскрывает BOM и резервирует остаток.
6. Проведённые в 1С документы остаются связаны со старым MRP.
7. Публикация выполняется одним generation switch; до него виден старый truth.

## Этапы

### 1. Successor lifecycle

- lineage `old plan/run -> successor plan/run`;
- причина закрытия `specification_rebase`;
- снимок исполнения старого run;
- создание строк successor-плана на остаток;
- атомарный `retire + add` через obligation refresh.

### 2. Ревизии спецификаций

- immutable content hash состава и операций;
- точная ревизия в `MrpFreezeComponent.spec_version`;
- история ревизий без перезаписи старой BOM.

### 3. Impact queue

- sync автоматически кладёт изменённые ревизии в durable pending queue;
- повторные изменения объединяются;
- worker не запускает один тяжёлый MRP на каждую строку sync.

### 4. Автоматический worker

- один advisory lock;
- idempotency key от parent generation, predecessor run и spec hashes;
- очередь новых изменений, пришедших во время build;
- падение build не меняе current planning truth.

### 5. Замер и расписание

Частота не фиксируется до пробного shadow-пересчёта. Замеряются отдельно:

- fork физического префикса;
- freeze/BOM;
- reservation materialization;
- historical reservation replay;
- построение read snapshots;
- полное wall-clock время.

После замера выбираются debounce, рабочее окно и максимальный размер пакета.

## Критерии приёмки

- 10 план, 8 принято: successor содержит 2;
- полностью выпущенная строка не попадает в successor;
- old run и его execution snapshot неизменны;
- old reservations не попадают в новую generation;
- successor имеет новые freeze, reservations и journals;
- проведённые 1С заказы не мутируют;
- retry не создаёт второй successor;
- ошибка до publish оставляет старую generation принятой.
