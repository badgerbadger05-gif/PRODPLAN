# 1С / OData

Backend синхронизирует справочники из 1С в PostgreSQL через OData.

Реализация:
- OData клиент: `backend/app/services/odata_client.py`
- Сервисы синка: `backend/app/services/*_sync.py`
- API синка: `backend/app/routers/sync.py`

Ключевые справочники:
- `Catalog_Сотрудники` -> `employees` через `POST /api/v1/sync/employees-odata`
- `Catalog_ЕдиницыИзмерения` -> `units`
- `Catalog_КатегорииНоменклатуры` -> `item_categories`
- `Catalog_Спецификации_Операции` -> `operations`

Особенности:
- Физические движения загружаются pull-by-document в `stock_ledger_entry`.
- Ресурс **Balance** используется только для начального якоря и сверки
  полноты Ledger. Balance не является источником планового остатка.
- Данные приходят пакетами, требуется аккуратная обработка (dry_run, прогресс)
- Дочерние документы, которые PRODPLAN создаёт в 1С, должны создаваться “на основании”: payload должен содержать `ДокументОснование` и `ДокументОснование_Type`.
- Первичные документы из MRP (`Document_ЗаказНаПроизводство`, `Document_ЗаказПоставщику`) создаются без 1С-основания.
- Перед созданием сдельных нарядов должна быть выполнена синхронизация сотрудников из `Catalog_Сотрудники`, чтобы исполнитель выбирался по локальной записи с сохраненным `Ref_Key`.
- Проведение документов через OData нельзя делать прямым `PATCH Posted=true`: это меняет флаг без гарантии движений регистров. Для `Document_ПеремещениеЗапасов` подтверждён рабочий сценарий `POST .../Unpost`, затем `POST .../Post?PostingModeOperational=true` при корректной текущей дате документа.

Подтверждённые основания для write-сценариев:

| Документ | Основание |
|---|---|
| `Document_ЗаказНаПроизводство` | из MRP — первичный, без основания; в цепочке «окраска→сварка» сварочный заказ создаётся на основании окрасочного: `ЗаказНаПроизводствоОснование_Key` (Edm.Guid, специализированное поле) + `ДокументОснование`/`_Type` |
| `Document_ПеремещениеЗапасов` | `StandardODATA.Document_ЗаказНаПроизводство` |
| `Document_СборкаЗапасов` | `StandardODATA.Document_ЗаказНаПроизводство` |
| `Document_СдельныйНаряд` | `StandardODATA.Document_СборкаЗапасов` |
| `Document_ЗаказПоставщику` | первичный документ без основания |

## Кодирование URL (важно!)

**Проблема (2026-02-26):** 1С игнорировала фильтры OData, если пробелы в URL не были закодированы как `%20`.

**Решение:** в `OData1CClient._make_request()` параметры кодируются вручную:
```python
# НЕ используйте urllib.parse.urlencode() — он не кодирует пробелы!
query_parts = []
for key, value in params.items():
    key_encoded = urllib.parse.quote(str(key), safe='')
    value_encoded = urllib.parse.quote(str(value), safe="$,()*'")
    value_encoded = value_encoded.replace(' ', '%20')  # обязательно!
    query_parts.append(f"{key_encoded}={value_encoded}")
query_string = "&".join(query_parts)
```

## Фильтры OData

1С УНФ корректно обрабатывает:
- ✅ `Posted eq true`
- ✅ `СостояниеЗаказа_Key ne guid'...'`

1С УНФ **игнорирует**:
- ❌ `DeletionMark eq false` (возвращает все заказы, включая удалённые)

**Решение:** фильтровать `DeletionMark` в коде (post-фильтрация). См. `production_order_sync.py`.

## Источник остатков

Физический `stock_bin` принятого Item Ledger — единственный источник остатков
для планирования.
