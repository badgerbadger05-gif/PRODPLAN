# 1С / OData

Backend синхронизирует справочники из 1С в PostgreSQL через OData.

Реализация:
- OData клиент: `backend/app/services/odata_client.py`
- Сервисы синка: `backend/app/services/*_sync.py`
- API синка: `backend/app/routers/sync.py`

Особенности:
- Остатки в 1С читаются через ресурс регистра **Balance**, а не через движения
- Данные приходят пакетами, требуется аккуратная обработка (dry_run, прогресс)

