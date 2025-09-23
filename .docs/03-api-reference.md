# PRODPLAN: Справочник API и команд

**Версия:** 1.6  
**Дата:** 2025-09-16

## 💻 CLI команды (`main.py`)

### Инициализация

#### `init-db` — Создание базы данных
```bash
python main.py init-db [--db PATH]
```
**Параметры:**
- `--db PATH` — Путь к SQLite БД (по умолчанию: `data/specifications.db`)

**Результат:** Создает все таблицы, индексы и триггеры

---

### Синхронизация данных

#### `sync-stock-odata` — Остатки из 1С OData
```bash
python main.py sync-stock-odata --url URL --entity ENTITY [OPTIONS]
```
**Обязательные параметры:**
- `--url URL` — URL OData API 1С  
- `--entity ENTITY` — Имя сущности (например: `AccumulationRegister_ЗапасыНаСкладах`)

**Дополнительные параметры:**
- `--auth-type basic|bearer` — Тип аутентификации
- `--username USER` — Логин для Basic Auth
- `--password PASS` — Пароль для Basic Auth  
- `--token TOKEN` — Bearer токен
- `--dry-run` — Режим предпросмотра без изменений

**Пример:**
```bash
python main.py sync-stock-odata \
  --url "http://srv-1c:8080/base/odata/standard.odata" \
  --entity "AccumulationRegister_ЗапасыНаСкладах" \
  --auth-type basic \
  --username admin \
  --password password123
```

#### `sync-stock` — Остатки из Excel файлов
```bash
python main.py sync-stock --dir DIRECTORY [OPTIONS]
```
**Параметры:**
- `--dir DIRECTORY` — Папка с Excel файлами остатков
- `--dry-run` — Режим предпросмотра

#### `sync-stock-history` — Остатки с сохранением истории
```bash
python main.py sync-stock-history --dir DIRECTORY [OPTIONS]
```
**Особенности:**
- Сохраняет снимок остатков в таблицу `stock_history`
- Автоматически удаляет записи старше 30 дней
- Позволяет анализировать тренды потребления

#### `sync-specs` — Спецификации из Excel
```bash
python main.py sync-specs --dir DIRECTORY [OPTIONS]
```
**Параметры:**
- `--dir DIRECTORY` — Папка с Excel спецификациями (по умолчанию: `specs/`)
- `--map-empty-stage-to STAGE` — Замена пустых этапов (по умолчанию: "Закупка")

---

### Генерация отчетов

#### `generate-plan` — План производства (Excel)
```bash
python main.py generate-plan --out OUTPUT [OPTIONS]
```
**Параметры:**
- `--out OUTPUT` — Путь к выходному файлу Excel
- `--days DAYS` — Количество дней планирования (по умолчанию: 30)
- `--start-date YYYY-MM-DD` — Дата начала планирования

**Результат:** 
- Главный лист с планом производства
- Отдельные листы по этапам производства
- Лист настроек планирования

#### `calculate-orders` — Расчет заказов
```bash
python main.py calculate-orders --output DIRECTORY [OPTIONS]
```
**Параметры:**
- `--output DIRECTORY` — Папка для сохранения файлов заказов

**Результат:**
- `orders_production.xlsx` — Заказы на производство (по статусам RED/BLUE/GREEN)
- `orders_purchase.xlsx` — Заказы на закупку

---

## 🌐 Веб-интерфейс (Quasar)

### Запуск UI
```bash
# Windows
run_ui.bat

# Linux/Mac
npm run dev

# С параметрами
npm run dev --port 9000
```

### Страницы интерфейса

#### 📋 "План производства"
**Функции:**
- Редактирование дневных планов по изделиям
- Автосохранение изменений в БД (`production_plan_entries`)
- Расчет "План на месяц" как сумма по дням
- Экспорт данных в CSV

**Кнопки управления:**
- "Обновить спецификации" — запуск `sync-specs`
- "Обновить остатки" — запуск `sync-stock`
- Отображение времени последнего обновления остатков

#### 🏭 "Этапы"
**Функции:**
- Просмотр плана по выбранному этапу производства
- Группировка по изделиям с подзаголовками
- Режим только для чтения (read-only)

---

## 📁 Batch скрипты (Windows)

### Основные операции
```bash
init_db.bat                 # Инициализация БД
sync_stock.bat              # Синхронизация остатков из Excel  
sync_stock_history.bat      # Синхронизация с историей
generate_plan.bat           # Генерация плана производства
calculate_orders.bat        # Расчет заказов
run_ui.bat                  # Запуск веб-интерфейса
```

### Настройки кодировки в batch файлах:
```batch
@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
```

---

## 🔌 Python API

### Основные функции для разработчиков

#### Подключение к БД
```python
from src.database import get_connection

conn = get_connection("data/specifications.db")
cursor = conn.cursor()
```

#### Расчет потребностей BOM
```python
from src.bom_calculator import explode_bom_for_root

# Развертка спецификации для корневого изделия
components = explode_bom_for_root(conn, root_item_id=123, quantity=10)
```

#### Синхронизация остатков
```python
from src.stock_sync import sync_stock

# Загрузка остатков из папки
result = sync_stock(conn, directory="ostatki/", dry_run=False)
```

#### Генерация плана
```python
from src.planner import generate_production_plan

# Создание Excel файла плана
generate_production_plan(
    conn, 
    output_file="output/plan.xlsx", 
    days=30,
    start_date="2025-09-16"
)
```

---

## 📊 Форматы данных

### Excel — План производства
**Структура листов:**
- **"План производства"** — основная таблица с колонками:
  - Наименование, Артикул
  - Выполнено, Недовыполнено, План на месяц  
  - 30 колонок дат (редактируемые)
  
- **Листы этапов** — отдельный лист для каждого этапа:
  - 8 колонок с подзаголовками по изделиям
  - Группировка компонентов под изделиями

- **"Таблица настроек"** — конфигурация этапов:
  - Выпадающие списки Да/Нет для каждого этапа

### Excel — Заказы
**orders_production.xlsx:**
- Листы по статусам: RED, BLUE, GREEN
- Колонки: Артикул, Наименование, Количество, Срок, Статус

**orders_purchase.xlsx:**
- Заказы на закупку материалов
- Группировка по поставщикам

### CSV экспорт из UI
- UTF-8 кодировка
- Разделитель: запятая
- Экспорт текущего представления плана

---

## ⚙️ Конфигурация

### Переменные окружения
```bash
export PRODPLAN_DB_PATH="data/specifications.db"
export PRODPLAN_SPECS_DIR="specs/"
export PRODPLAN_STOCK_DIR="ostatki/"
```

### Файл `requirements.txt`
```
openpyxl>=3.1.2
pandas>=2.1.0  
numpy>=1.23.0
python-dateutil>=2.8.2
requests>=2.31.0
```