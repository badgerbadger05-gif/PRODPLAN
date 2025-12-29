# 1С / OData (кратко)

Идея: backend ходит в OData 1С и синхронизирует справочники в PostgreSQL.

Где смотреть реализацию:

- OData клиент: `backend/app/services/odata_client.py`
- Сервисы синка: `backend/app/services/*_sync.py`
- API синка: `backend/app/routers/sync.py`

Что важно помнить:

- Остатки в 1С читаются через ресурс регистра **Balance**, а не через движения.
- У 1С данные часто приходят пакетами и требуют аккуратной обработки (dry_run, прогресс).

