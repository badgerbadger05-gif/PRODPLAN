# API

Источник правды: Swagger UI http://localhost:8000/docs (OpenAPI: `/openapi.json`).
Схемы — `backend/app/schemas.py`, роуты — `backend/app/routers/` (16 модулей). Контракты не ломаем без явного решения.

Нормативный контракт `docs/api/CONTRACT.md` живёт на ветке `refactor/cleanup-1` и приедет с мерджем.

## Группы роутеров (все под `/api`)

| Модуль | Префикс | Назначение |
|---|---|---|
| `items.py` | `/v1/items` | номенклатура (CRUD/поиск) |
| `nomenclature.py` | `/v1/nomenclature` | группы/дерево номенклатуры |
| `stages.py` | `/v1/stages` | этапы производства |
| `resources.py` | `/v1/resources` | участки/ресурсы, мощности |
| `specification.py` | `/v1/specification` | дерево спецификаций (BOM) |
| `specification_repair.py` | `/v1/specification-repair` | починка/сверка спецификаций |
| `plan.py` | `/v1/plan` | прогоны MRP, результаты, период-план, экспорт в 1С |
| `sync.py` | `/v1/sync` | синхронизация из 1С (OData), оркестратор |
| `odata.py` | `/v1/odata` | конфигурация OData-подключения (секреты маскируются) |
| `production_control.py` | `/v1/production-control` | журнал заказов производства, произвести/переместить, экспорт в 1С |
| `production_control_settings.py` | (внутри production-control) | настройки производственного контроля |
| `purchase_control.py` | `/v1/purchase-control` | журнал закупок (заказы поставщику + MRP to_order) |
| `workshop_binding_review.py` | `/v1/workshop-binding-review` | ревью привязок цех ↔ склад |
| `dbr.py` | `/v1/dbr` | модуль DBR: барабан, питатели, программы, доски |
| `paint_weld.py` | `/v1/paint-weld` | связка «окраска ↔ сварка» |
| `item_ledger.py` | `/v1/item-ledger` | read-API карточки номенклатуры (позиция/движения/резервы/дрейф) |
