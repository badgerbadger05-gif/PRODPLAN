# Диагностика и типовые операции

## Docker

Запуск:

```bash
docker-compose up -d
```

Остановка:

```bash
docker-compose down
```

Логи:

```bash
docker-compose logs -f backend
docker-compose logs -f frontend
docker-compose logs -f db
```

## Alembic / миграции

Проверка текущей версии:

```bash
docker-compose exec backend alembic current
```

Применить миграции:

```bash
docker-compose exec backend alembic upgrade head
```

## Тесты

```bash
docker-compose exec backend pytest
```

## Быстрые проверки API

- Swagger: http://localhost:8000/docs
- Healthcheck (если есть): смотрим `/docs` + логи backend

