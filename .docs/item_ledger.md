# Item Ledger

Нормативная модель:

- [`CANON.md`](CANON.md);
- [`reservation-replenishment-core.md`](reservation-replenishment-core.md);
- [`planning-truth-contract.md`](planning-truth-contract.md);
- `/home/ivan/PRODPLAN/mrp-item-ledger-design.md`;
- `/home/ivan/PRODPLAN/item-ledger-view-contract.md`.

1С читается pull-by-document в `stock_ledger_entry`. `stock_bin` хранит
материализованный остаток. Balance служит только сверкой и источником
начального якоря, но не плановым остатком.

Одна `ReservationEntry` соответствует одному требованию. Она хранит полный
резерв, покрытие наличием на freeze, зафиксированную потребность пополнения и
FIFO-пополнения. `make` и `buy` выбирают рабочий журнал; расчёт общий.

Физический выпуск готового изделия тем же фактом уменьшает остаток
соответствующей строки общей очереди сборки. Контракт очереди:
[`assembly-queue-and-drum.md`](assembly-queue-and-drum.md).

Тяжёлые операции строят кандидат, проверяют его и атомарно публикуют принятое
поколение. Читатели не пересчитывают данные.

Запрещены legacy stock, повторный расчёт BOM при refresh, двойное назначение
факта, количественный гейт по provenance и fallback при отсутствии Ledger.

Планировщики читают остатки только из принятого поколения Item Ledger.
