# Архитектура

## Компоненты

- **Frontend** (Quasar/Vue): SPA на порту 9000
- **Backend** (FastAPI): REST API на порту 8000
- **DB** (PostgreSQL): хранение данных + результатов MRP
- **Интеграция 1С**: OData (синхронизация справочников и остатков)

## Структура кода

Backend:
- `backend/app/main.py` — приложение FastAPI, регистрация роутеров
- `backend/app/routers/` — слой HTTP (эндпоинты)
- `backend/app/services/` — бизнес-логика (MRP, синхронизации, экспорт)
- `backend/app/models.py` — SQLAlchemy модели
- `backend/alembic/` — миграции

Frontend:
- `frontend/src/pages/` — страницы
- `frontend/src/components/` — компоненты
- `frontend/src/services/api.ts` — единая точка вызова backend API

## MRP конвейер

1) загрузка конфигурации и входных данных
2) расчёт gross/net потребностей
3) формирование заказов производства/закупки
4) планирование мощностей
5) pegging + приоритезация
6) сохранение результатов в таблицы `planned_*` и `capacity_load`

