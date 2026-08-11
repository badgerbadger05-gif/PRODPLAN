# Документация PRODPLAN

В этой папке хранится только действующая модель. Исторические варианты и
отменённые архитектуры не сохраняются рядом с каноном.

## Читать в этом порядке

1. [`CANON.md`](CANON.md) — владельцы величин и запреты.
2. [`reservation-replenishment-core.md`](reservation-replenishment-core.md) —
   количество, резервы, потребности и адресно-FIFO выполнение.
3. [`assembly-queue-and-drum.md`](assembly-queue-and-drum.md) — единая очередь
   сборки и календарная раскладка.
4. [`planning-truth-contract.md`](planning-truth-contract.md) — граница
   обязательства и физического факта.
5. [`shelves-buffers-and-mechshop-pull.md`](shelves-buffers-and-mechshop-pull.md)
   — динамические полки и приоритет мехцеха.
6. [`period_plan_target.md`](period_plan_target.md) — жизненный цикл плана.
7. [`unified_production_journal.md`](unified_production_journal.md) —
   производственный и закупочный журналы.
8. [`architecture.md`](architecture.md) — общий поток данных.
9. [`frontend-framework.md`](frontend-framework.md) — типизированный общий
   каркас экранов и правила постепенного переиспользования.

## Справочные документы

- `item_ledger.md` — устройство физической и резервной лент;
- `db_schema.md` — отображение канона на таблицы;
- `api.md` — группы API;
- `odata.md` и `one_c_export_from_prodplan.md` — граница с 1С;
- `troubleshooting.md` — эксплуатационная диагностика.

Документ, противоречащий перечисленному канону, подлежит удалению, а не
переносу в раздел «история».
