# Диагностика и операции

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

Текущая версия:
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

## API

Swagger: http://localhost:8000/docs

