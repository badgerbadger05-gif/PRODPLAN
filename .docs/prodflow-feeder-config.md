# One-time ProdFlow feeder configuration

Источник исходного снимка — активные строки ERPNext DocType
`ProdFlow Supermarket Position` (`item`, `warehouse`, `is_active`,
`route_class`) и справочник `Warehouse` (`name`, `custom_ref1c`). Это разовый
переходный импорт, а не постоянная интеграция.

Канонический writer:

```powershell
python tools/import_prodflow_feeders.py `
  --positions <feeder-targets.json> `
  --warehouses <warehouse-refs.json> `
  --expected-count 995 `
  --expected-db-host <compose-db-service> `
  --expected-db-name prodplan `
  --observed-at 2026-10-29T15:51:00+03:00 `
  --output <dry-run-receipt.json>
```

Без `--apply` команда выполняет полную проверку и rollback. С `--apply` она
атомарно заменяет только reference assignment и manifest. Проверяются:

- точное число активных строк;
- один feeder на item, без дублей;
- наличие каждого item в PRODPLAN;
- `Warehouse.custom_ref1c` и наличие ref в `StockWarehouse`;
- детерминированный SHA-256 revision.

`DATABASE_URL` обязан быть явно передан окружением release-контейнера;
встроенный localhost default запрещён. Host и имя базы должны совпасть с
обязательными `--expected-db-host/--expected-db-name`, а receipt содержит
только безопасное `host/database`, без credentials. Входные JSON лежат во
внешнем защищённом каталоге release (не в git archive) и передаются контейнеру
read-only. Нулевой/пустой снимок не является командой очистки и отклоняется.

Остатки, custody, планы и документы 1С команда не читает и не изменяет.
Повтор того же снимка идемпотентен. Изменившийся checksum инвалидирует текущий
production journal до штатной перепубликации material snapshot. Возраст
снимка сам по себе не делает конфигурацию недоступной; новое копирование —
только по отдельному решению владельца.
