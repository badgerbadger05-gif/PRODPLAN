# API (кратко)

Источник правды по API:

- Swagger UI: http://localhost:8000/docs
- OpenAPI JSON: `openapi.json`

Принципы:

- Контракты стараемся **не ломать**.
- Все request/response модели — в `backend/app/schemas.py`.
- Все роуты — в `backend/app/routers/`.

Базовые группы эндпоинтов:

- `/api/v1/sync/*` — синхронизация данных из 1С (OData)
- `/api/v1/specification/*` — дерево спецификаций (BOM)
- `/api/v1/plan/*` — прогоны MRP и результаты

