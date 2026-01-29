# Как работать с проектом через ИИ

Этот репозиторий **пишется нейросетями**. От владельца проекта — логика и бизнес-правила.

## Формат задачи ИИ

1) **Что изменить** (одной фразой)
2) **Контракт** (поля/эндпоинты/страницы)
3) **Ограничения** (что нельзя трогать)
4) **Критерии готовности**

Пример:
> Добавь фильтр по складу в синхронизацию остатков.
> Контракт: новый query-параметр warehouse_id.
> Ограничения: не ломать текущие запросы без warehouse_id.
> Готово: есть тест + swagger показывает параметр.

## Инварианты разработки

- Backend: FastAPI + SQLAlchemy + Alembic
- Frontend: Quasar (Vue 3 + TS)
- Слои:
  - HTTP/валидация: `backend/app/routers/`
  - бизнес-логика: `backend/app/services/`
  - схемы API: `backend/app/schemas.py`
  - ORM/таблицы: `backend/app/models.py`

## Правила изменений

1) Любая правка таблиц в `models.py` → **новая миграция Alembic**
2) Любая новая логика в backend → **pytest**
3) Новая интеграция во фронте → через `frontend/src/services/api.ts`
4) После завершения задачи — обновить `progress.md`

## Команды

Docker:
```bash
docker-compose up -d
docker-compose logs -f backend
```

Тесты:
```bash
docker-compose exec backend pytest
```

