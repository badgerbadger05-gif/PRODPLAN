# План миграции PRODPLAN на Quasar + FastAPI + PostgreSQL

**Версия:** 1.0
**Дата:** 2025-09-20
**Статус:** Планирование

## 📋 Обзор миграции

Проект PRODPLAN в настоящее время использует архитектуру на базе SQLite + NiceGUI + встроенный FastAPI. План миграции предусматривает переход на более масштабируемую и современную архитектуру с разделением ответственности между компонентами.

### 🎯 Цели миграции

1. **Масштабируемость**: Переход с SQLite на PostgreSQL для поддержки больших объемов данных
2. **Производительность**: Разделение frontend и backend для лучшей производительности
3. **Современный UI**: Переход на Quasar (Vue.js) для более гибкого и современного интерфейса
4. **Микросервисная архитектура**: Разделение API и UI на отдельные сервисы
5. **Улучшенная поддержка**: Лучшая типизация, тестируемость и документация

---

## 🏗️ Текущая архитектура

### Компоненты
- **База данных**: SQLite (файл `data/specifications.db`)
- **Backend**: FastAPI (встроенный в NiceGUI)
- **Frontend**: NiceGUI (Python-based UI фреймворк)
- **API**: REST API через FastAPI с эндпоинтами в `/api/*`

### Структура проекта
```
prodplan/
├── src/
│   ├── database.py          # SQLite подключения и схема
│   ├── planner.py           # Генерация Excel планов
│   ├── bom_calculator.py    # Расчеты спецификаций
│   ├── odata_client.py      # 1С интеграция
│   └── ui_nicegui/          # NiceGUI приложение
│       ├── app.py           # FastAPI + NiceGUI
│       ├── routes.py        # UI страницы
│       └── services/        # Бизнес-логика
├── data/
│   └── specifications.db    # SQLite БД
└── requirements.txt
```

---

## 🆕 Целевая архитектура

### Компоненты
- **База данных**: PostgreSQL
- **Backend API**: Отдельный FastAPI сервер
- **Frontend**: Quasar (Vue.js 3 + TypeScript)
- **Коммуникация**: REST API + WebSocket (опционально)

### Структура проекта
```
prodplan/
├── backend/                 # FastAPI сервер
│   ├── app/                 # Основное приложение
│   │   ├── main.py          # Точка входа
│   │   ├── database.py      # PostgreSQL подключения
│   │   ├── models.py        # Pydantic модели
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
│   └── types.ts
├── docker/                  # Docker конфигурация
├── docs/                    # Документация
└── scripts/                 # Скрипты развертывания
```

---

## 📊 Миграция базы данных: SQLite → PostgreSQL

### Существующая схема SQLite

**Основные таблицы:**
- `production_stages` — Этапы производства
- `items` — Номенклатура (код, название, артикул, остатки)
- `bom` — Спецификации (родитель-ребенок связи)
- `production_plan_entries` — Планы производства
- `stock_history` — История остатков
- `root_products` — Корневые изделия

### PostgreSQL схема

```sql
-- Производственные этапы
CREATE TABLE production_stages (
    stage_id SERIAL PRIMARY KEY,
    stage_name VARCHAR(255) UNIQUE NOT NULL,
    stage_order INTEGER,
    stage_ref1c VARCHAR(36), -- GUID из 1С
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Номенклатура
CREATE TABLE items (
    item_id SERIAL PRIMARY KEY,
    item_code VARCHAR(50) UNIQUE NOT NULL,
    item_name TEXT NOT NULL,
    item_article VARCHAR(100),
    item_ref1c VARCHAR(36), -- GUID из 1С
    replenishment_method VARCHAR(50),
    replenishment_time INTEGER,
    unit VARCHAR(50),
    stock_qty DECIMAL(10,3) DEFAULT 0.0,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Спецификации (Bill of Materials)
CREATE TABLE bom (
    bom_id SERIAL PRIMARY KEY,
    parent_item_id INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    child_item_id INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    quantity DECIMAL(10,3) NOT NULL,
    link_stage_id INTEGER REFERENCES production_stages(stage_id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(parent_item_id, child_item_id)
);

-- Планы производства
CREATE TABLE production_plan_entries (
    id SERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    stage_id INTEGER REFERENCES production_stages(stage_id) ON DELETE SET NULL,
    date DATE NOT NULL,
    planned_qty DECIMAL(10,3) NOT NULL DEFAULT 0.0,
    completed_qty DECIMAL(10,3) NOT NULL DEFAULT 0.0,
    status VARCHAR(20) NOT NULL DEFAULT 'GREEN',
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(item_id, stage_id, date)
);

-- История остатков
CREATE TABLE stock_history (
    id SERIAL PRIMARY KEY,
    item_code VARCHAR(50) NOT NULL,
    stock_qty DECIMAL(10,3) NOT NULL,
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Корневые изделия
CREATE TABLE root_products (
    item_id INTEGER PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE
);
```

### Индексы для производительности

```sql
-- Индексы для быстрого поиска
CREATE INDEX idx_items_code ON items(item_code);
CREATE INDEX idx_items_article ON items(item_article);
CREATE INDEX idx_items_ref1c ON items(item_ref1c);

-- Индексы для спецификаций
CREATE INDEX idx_bom_parent ON bom(parent_item_id);
CREATE INDEX idx_bom_child ON bom(child_item_id);
CREATE INDEX idx_bom_parent_child ON bom(parent_item_id, child_item_id);

-- Индексы для планов
CREATE INDEX idx_plan_item_date ON production_plan_entries(item_id, date);
CREATE INDEX idx_plan_stage_date ON production_plan_entries(stage_id, date);
CREATE INDEX idx_plan_date ON production_plan_entries(date);

-- Индексы для истории
CREATE INDEX idx_stock_history_item_date ON stock_history(item_code, recorded_at);
```

### Миграция данных

#### 1. Подготовка миграции
```bash
# Установка инструментов
pip install alembic psycopg2-binary

# Инициализация Alembic
cd backend
alembic init alembic
```

#### 2. Конфигурация подключения
```python
# backend/app/database.py
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://user:password@localhost:5432/prodplan"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
```

#### 3. SQLAlchemy модели
```python
# backend/app/models.py
from sqlalchemy import Column, Integer, String, DECIMAL, TIMESTAMP, ForeignKey, TEXT
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base

class ProductionStage(Base):
    __tablename__ = "production_stages"

    stage_id = Column(Integer, primary_key=True, index=True)
    stage_name = Column(String(255), unique=True, nullable=False)
    stage_order = Column(Integer)
    stage_ref1c = Column(String(36))
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())

class Item(Base):
    __tablename__ = "items"

    item_id = Column(Integer, primary_key=True, index=True)
    item_code = Column(String(50), unique=True, nullable=False, index=True)
    item_name = Column(TEXT, nullable=False)
    item_article = Column(String(100), index=True)
    item_ref1c = Column(String(36), index=True)
    replenishment_method = Column(String(50))
    replenishment_time = Column(Integer)
    unit = Column(String(50))
    stock_qty = Column(DECIMAL(10, 3), default=0.0)
    status = Column(String(20), default='active')
    created_at = Column(TIMESTAMP, default=func.now())
    updated_at = Column(TIMESTAMP, default=func.now(), onupdate=func.now())
```

#### 4. Pydantic схемы
```python
# backend/app/schemas.py
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime

class ItemBase(BaseModel):
    item_code: str
    item_name: str
    item_article: Optional[str] = None
    item_ref1c: Optional[str] = None
    replenishment_method: Optional[str] = None
    replenishment_time: Optional[int] = None
    unit: Optional[str] = None
    stock_qty: float = 0.0
    status: str = 'active'

class ItemCreate(ItemBase):
    pass

class ItemUpdate(ItemBase):
    pass

class Item(ItemBase):
    item_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
```

#### 5. Скрипт миграции
```python
# scripts/migrate_from_sqlite.py
import sqlite3
import psycopg2
from pathlib import Path
import os

def migrate_sqlite_to_postgres():
    # Подключение к SQLite
    sqlite_path = Path("data/specifications.db")
    sqlite_conn = sqlite3.connect(sqlite_path)

    # Подключение к PostgreSQL
    pg_conn = psycopg2.connect(os.getenv("DATABASE_URL"))

    try:
        # Миграция items
        sqlite_items = sqlite_conn.execute("SELECT * FROM items").fetchall()
        pg_cursor = pg_conn.cursor()

        for item in sqlite_items:
            pg_cursor.execute("""
                INSERT INTO items (item_code, item_name, item_article, item_ref1c,
                                 replenishment_method, replenishment_time, unit,
                                 stock_qty, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (item_code) DO UPDATE SET
                    item_name = EXCLUDED.item_name,
                    item_article = EXCLUDED.item_article,
                    updated_at = CURRENT_TIMESTAMP
            """, item)

        # Миграция других таблиц аналогично...

        pg_conn.commit()
        print("Миграция завершена успешно")

    except Exception as e:
        pg_conn.rollback()
        print(f"Ошибка миграции: {e}")
    finally:
        sqlite_conn.close()
        pg_conn.close()
```

---

## 🔧 Миграция Backend: NiceGUI → FastAPI

### Структура FastAPI приложения

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py              # Точка входа FastAPI
│   ├── config.py            # Конфигурация
│   ├── database.py          # PostgreSQL подключения
│   ├── models.py            # SQLAlchemy модели
│   ├── schemas.py           # Pydantic схемы
│   ├── routers/             # API роутеры
│   │   ├── __init__.py
│   │   ├── items.py         # CRUD для номенклатуры
│   │   ├── plans.py         # Планы производства
│   │   ├── stages.py        # Этапы производства
│   │   ├── odata.py         # 1С интеграция
│   │   └── reports.py       # Отчеты
│   └── services/            # Бизнес-логика
│       ├── __init__.py
│       ├── plan_service.py  # Сервисы планирования
│       ├── bom_service.py   # Спецификации
│       └── odata_service.py # 1С клиент
├── alembic/                 # Миграции БД
├── tests/                   # Тесты
├── requirements.txt
└── Dockerfile
```

### Основные API эндпоинты

#### Items API
```python
# backend/app/routers/items.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import Item
from ..schemas import Item, ItemCreate, ItemUpdate

router = APIRouter(prefix="/items", tags=["items"])

@router.get("/", response_model=List[Item])
def get_items(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    items = db.query(Item).offset(skip).limit(limit).all()
    return items

@router.post("/", response_model=Item)
def create_item(item: ItemCreate, db: Session = Depends(get_db)):
    db_item = Item(**item.dict())
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item

@router.get("/{item_id}", response_model=Item)
def get_item(item_id: int, db: Session = Depends(get_db)):
    db_item = db.query(Item).filter(Item.item_id == item_id).first()
    if db_item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return db_item
```

#### Plans API
```python
# backend/app/routers/plans.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import date
from ..database import get_db
from ..models import ProductionPlanEntry, Item
from ..schemas import PlanEntry, PlanEntryCreate

router = APIRouter(prefix="/plans", tags=["plans"])

@router.get("/matrix", response_model=List[PlanEntry])
def get_plan_matrix(
    start_date: date,
    days: int = 30,
    stage_id: int = None,
    db: Session = Depends(get_db)
):
    # Логика получения матрицы плана
    pass

@router.post("/upsert")
def upsert_plan_entry(
    entry: PlanEntryCreate,
    db: Session = Depends(get_db)
):
    # Логика upsert записи плана
    pass
```

### Конфигурация и окружение

```python
# backend/app/config.py
from pydantic import BaseSettings

class Settings(BaseSettings):
    app_name: str = "PRODPLAN API"
    version: str = "1.0.0"
    database_url: str
    odata_base_url: str = ""
    odata_username: str = ""
    odata_password: str = ""

    class Config:
        env_file = ".env"

settings = Settings()
```

---

## 🎨 Миграция Frontend: NiceGUI → Quasar

### Структура Quasar приложения

```
frontend/
├── src/
│   ├── components/          # Переиспользуемые компоненты
│   │   ├── PlanTable.vue   # Таблица плана производства
│   │   ├── ItemSearch.vue  # Поиск номенклатуры
│   │   └── StageFilter.vue # Фильтр по этапам
│   ├── views/              # Страницы
│   │   ├── PlanView.vue    # План производства
│   │   ├── StagesView.vue  # Этапы производства
│   │   └── SettingsView.vue # Настройки
│   ├── stores/             # Pinia stores
│   │   ├── plan.ts         # Store для плана
│   │   ├── items.ts        # Store для номенклатуры
│   │   └── settings.ts     # Store для настроек
│   ├── services/           # API сервисы
│   │   ├── api.ts          # Базовый API клиент
│   │   ├── planService.ts  # Сервис плана
│   │   └── itemService.ts  # Сервис номенклатуры
│   ├── types/              # TypeScript типы
│   │   ├── plan.ts         # Типы плана
│   │   ├── item.ts         # Типы номенклатуры
│   │   └── api.ts          # API типы
│   ├── router/             # Маршрутизация
│   │   └── index.ts
│   ├── boot/               # Загрузчики
│   │   └── axios.ts
│   └── layouts/            # Layouts
│       └── MainLayout.vue
├── package.json
├── quasar.config.js
└── tsconfig.json
```

### Основные компоненты

#### API клиент
```typescript
// frontend/src/services/api.ts
import axios from 'axios'

const api = axios.create({
  baseURL: process.env.API_URL || 'http://localhost:8000/api',
  timeout: 10000,
})

export interface ApiResponse<T> {
  data: T
  success: boolean
  message?: string
}

export default api
```

#### Store для плана
```typescript
// frontend/src/stores/plan.ts
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/services/api'
import type { PlanEntry, PlanMatrix } from '@/types/plan'

export const usePlanStore = defineStore('plan', () => {
  const planMatrix = ref<PlanMatrix | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const startDate = ref(new Date())
  const days = ref(30)

  const fetchPlanMatrix = async () => {
    loading.value = true
    error.value = null

    try {
      const response = await api.get('/plans/matrix', {
        params: {
          start_date: startDate.value.toISOString().split('T')[0],
          days: days.value
        }
      })

      planMatrix.value = response.data
    } catch (err) {
      error.value = 'Ошибка загрузки плана'
      console.error(err)
    } finally {
      loading.value = false
    }
  }

  const updatePlanEntry = async (entry: PlanEntry) => {
    try {
      await api.post('/plans/upsert', entry)
      // Обновить локальное состояние
      await fetchPlanMatrix()
    } catch (err) {
      console.error('Ошибка сохранения', err)
      throw err
    }
  }

  return {
    planMatrix,
    loading,
    error,
    startDate,
    days,
    fetchPlanMatrix,
    updatePlanEntry
  }
})
```

#### Компонент таблицы плана
```vue
<!-- frontend/src/components/PlanTable.vue -->
<template>
  <q-table
    :rows="planMatrix?.rows || []"
    :columns="columns"
    :loading="loading"
    :pagination="pagination"
    @request="onRequest"
    row-key="item_id"
    flat
    class="plan-table"
  >
    <template v-slot:body-cell-date="props">
      <q-td :props="props">
        <q-input
          v-model.number="props.value"
          type="number"
          step="1"
          min="0"
          dense
          @blur="updateCell(props.row, props.col)"
        />
      </q-td>
    </template>
  </q-table>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { usePlanStore } from '@/stores/plan'

const planStore = usePlanStore()

const columns = computed(() => [
  {
    name: 'item_name',
    label: 'Изделие',
    field: 'item_name',
    align: 'left',
    required: true
  },
  {
    name: 'item_article',
    label: 'Артикул',
    field: 'item_article',
    align: 'left'
  },
  // Динамические колонки по датам
  ...generateDateColumns()
])

const updateCell = async (row: any, col: any) => {
  if (col.name.startsWith('date_')) {
    const date = col.name.replace('date_', '')
    await planStore.updatePlanEntry({
      item_id: row.item_id,
      date: date,
      planned_qty: row[col.name] || 0
    })
  }
}
</script>
```

### Package.json зависимости

```json
{
  "name": "prodplan-frontend",
  "version": "1.0.0",
  "description": "PRODPLAN Frontend",
  "scripts": {
    "dev": "quasar dev",
    "build": "quasar build",
    "lint": "eslint --ext .js,.ts,.vue ./src",
    "format": "prettier --write \"**/*.{js,ts,vue,scss,html,md,json}\" --ignore-path .gitignore"
  },
  "dependencies": {
    "@quasar/extras": "^1.16.0",
    "axios": "^1.6.0",
    "pinia": "^2.1.0",
    "vue": "^3.3.0",
    "vue-router": "^4.2.0"
  },
  "devDependencies": {
    "@quasar/cli": "^2.3.0",
    "@types/node": "^20.0.0",
    "@typescript-eslint/eslint-plugin": "^6.0.0",
    "@typescript-eslint/parser": "^6.0.0",
    "eslint": "^8.50.0",
    "prettier": "^3.0.0",
    "typescript": "^5.2.0"
  }
}
```

---

## 🚀 Развертывание и инфраструктура

### Docker конфигурация

#### Backend Dockerfile
```dockerfile
# backend/Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

#### Frontend Dockerfile
```dockerfile
# frontend/Dockerfile
FROM node:18-alpine

WORKDIR /app

COPY package*.json ./
RUN npm install

COPY . .

RUN npm run build

EXPOSE 9000

CMD ["npm", "run", "dev"]
```

#### Docker Compose
```yaml
# docker-compose.yml
version: '3.8'

services:
  db:
    image: postgres:15
    environment:
      POSTGRES_DB: prodplan
      POSTGRES_USER: prodplan
      POSTGRES_PASSWORD: password
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  backend:
    build: ./backend
    environment:
      DATABASE_URL: postgresql://prodplan:password@db:5432/prodplan
    depends_on:
      - db
    ports:
      - "8000:8000"

  frontend:
    build: ./frontend
    environment:
      API_URL: http://localhost:8000/api
    ports:
      - "9000:9000"

volumes:
  postgres_data:
```

### CI/CD Pipeline

#### GitHub Actions
```yaml
# .github/workflows/deploy.yml
name: Deploy PRODPLAN

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
    - name: Install dependencies
      run: |
        cd backend
        pip install -r requirements.txt
    - name: Run tests
      run: |
        cd backend
        pytest

  build:
    needs: test
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3
    - name: Build and push Docker images
      run: |
        docker build -t prodplan-backend ./backend
        docker build -t prodplan-frontend ./frontend
        # Push to registry
```

### Мониторинг и логирование

#### Backend логирование
```python
# backend/app/main.py
import logging
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PRODPLAN API", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    logger.info("PRODPLAN API started")
```

#### Frontend логирование
```typescript
// frontend/src/boot/logger.ts
import { boot } from 'quasar/wrappers'

export default boot(({ app }) => {
  // Настройка логирования
  app.config.globalProperties.$log = console
})
```

---

## 📋 План выполнения миграции

### Этап 1: Подготовка (1-2 недели)
- [ ] Настройка PostgreSQL сервера
- [ ] Создание новой схемы БД
- [ ] Миграция данных из SQLite
- [ ] Настройка Docker окружения

### Этап 2: Backend миграция (2-3 недели)
- [ ] Создание FastAPI приложения
- [ ] Миграция SQLAlchemy моделей
- [ ] Перенос API эндпоинтов
- [ ] Тестирование API

### Этап 3: Frontend миграция (3-4 недели)
- [ ] Настройка Quasar проекта
- [ ] Создание основных компонентов
- [ ] Интеграция с API
- [ ] Тестирование UI

### Этап 4: Интеграция и тестирование (1-2 недели)
- [ ] Интеграционное тестирование
- [ ] Оптимизация производительности
- [ ] Документирование API
- [ ] Подготовка к развертыванию

### Этап 5: Развертывание (1 неделя)
- [ ] Настройка CI/CD
- [ ] Развертывание в продакшн
- [ ] Мониторинг и поддержка

---

## 🎯 Преимущества новой архитектуры

### Масштабируемость
- PostgreSQL поддерживает большие объемы данных
- Горизонтальное масштабирование API
- Оптимизированные запросы и индексы

### Производительность
- Разделение frontend и backend
- Асинхронные операции
- Кэширование на уровне API

### Разработка и поддержка
- Типизация на TypeScript
- Лучшее разделение ответственности
- Легче тестировать компоненты отдельно

### Пользовательский опыт
- Современный и быстрый UI
- Адаптивный дизайн
- Лучшая производительность интерфейса

---

## 📞 Рекомендации

1. **Начать с малого**: Сначала мигрировать только API, оставив NiceGUI как frontend
2. **Параллельная разработка**: Разрабатывать новый frontend параллельно с текущим
3. **Тестирование**: Тщательно тестировать каждый этап миграции
4. **Резервные копии**: Делать бэкапы данных перед каждым этапом
5. **Документация**: Вести документацию по API и компонентам

---

**Конец документа**

Этот план миграции обеспечивает плавный переход от текущей архитектуры к современной масштабируемой системе с разделением ответственности между компонентами.