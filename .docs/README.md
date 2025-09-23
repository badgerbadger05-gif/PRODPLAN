# PRODPLAN - Миграция на Quasar + FastAPI + PostgreSQL

Этот проект представляет собой миграцию существующего решения PRODPLAN с архитектуры SQLite + NiceGUI на современную архитектуру с разделением ответственности между компонентами.

## Архитектура

- **База данных**: PostgreSQL
- **Backend API**: Отдельный FastAPI сервер
- **Frontend**: Quasar (Vue.js 3 + TypeScript)
- **Коммуникация**: REST API

## Структура проекта

```
prodplan/
├── backend/                 # FastAPI сервер
│   ├── app/                 # Основное приложение
│   │   ├── main.py          # Точка входа
│   │   ├── database.py      # Подключения к PostgreSQL
│   │   ├── models.py        # Модели SQLAlchemy
│   │   ├── schemas.py       # Pydantic схемы
│   │   ├── routers/         # API роутеры
│   │   └── services/        # Бизнес-логика
│   ├── alembic/             # Миграции БД
│   └── requirements.txt
├── frontend/                # Quasar приложение
│   ├── src/
│   │   ├── components/      # Vue компоненты
│   │   ├── views/          # Страницы
│   │   ├── stores/         # Pinia stores
│   │   └── services/       # API сервисы
│   ├── package.json
│   └── quasar.config.js
├── shared/                  # Общие типы и утилиты
├── docker/                  # Docker конфигурация
├── docs/                    # Документация
└── scripts/                 # Скрипты развертывания
```

## Предварительные требования

- Установленный [Docker](https://docs.docker.com/get-docker/)
- Установленный [Docker Compose](https://docs.docker.com/compose/install/)

## Локализация и кодировка

Система разработана с учетом поддержки русского языка. Все текстовые данные в базе данных и интерфейсе пользователя представлены на русском языке. Для корректной работы с кириллическими символами используется кодировка UTF-8.

## Запуск проекта

### С помощью файлов запуска

Для Unix-систем:
```bash
./start.sh
```

Для Windows:
```cmd
start.bat
```

### С помощью Docker Compose

```bash
docker-compose up -d
```

Это запустит:
- PostgreSQL на порту 5432
- Backend (FastAPI) на порту 8000
- Frontend (Quasar) на порту 9000

### Ручной запуск

#### Backend

1. Установите зависимости:
   ```bash
   cd backend
   pip install -r requirements.txt
   ```

2. Запустите сервер:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

#### Frontend

1. Установите зависимости:
   ```bash
   cd frontend
   npm install
   ```

2. Запустите dev-сервер:
   ```bash
   npm run dev
   ```

## Разработка

### Backend

- Основная точка входа: `backend/app/main.py`
- Модели SQLAlchemy: `backend/app/models.py`
- Pydantic схемы: `backend/app/schemas.py`
- API роутеры: `backend/app/routers/`
- Бизнес-логика: `backend/app/services/`

### Frontend

- Основная точка входа: `frontend/src/main.ts`
- Компоненты: `frontend/src/components/`
- Страницы: `frontend/src/views/`
- Маршруты: `frontend/src/router/`
- Stores: `frontend/src/stores/`
- API сервисы: `frontend/src/services/`