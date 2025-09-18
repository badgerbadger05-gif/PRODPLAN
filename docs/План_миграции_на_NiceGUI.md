# Переход на NiceGUI: план миграции v1.0
Версия: 1.0
Дата: 2025-09-18

Цель
- Перевести веб-интерфейс со Streamlit на NiceGUI без изменений бизнес-логики и CLI.
- Сохранить совместимость с текущей структурой БД SQLite и потоками данных.
- Повысить UX, производительность и поддерживаемость.

Контекст и текущее состояние
- Архитектура и БД: см. [docs/02-architecture.md](docs/02-architecture.md).
- API и CLI: см. [docs/03-api-reference.md](docs/03-api-reference.md).
- Roadmap: см. [docs/05-roadmap.md](docs/05-roadmap.md).
- Текущий UI: Streamlit в [src/ui.py](src/ui.py).
- Ключевые модули ядра: [src/database.py](src/database.py), [src/planner.py](src/planner.py), [src/order_calculator.py](src/order_calculator.py), [src/bom_calculator.py](src/bom_calculator.py), [src/odata_client.py](src/odata_client.py), [src/odata_stock_sync.py](src/odata_stock_sync.py), [src/stock_history.py](src/stock_history.py), [main.py](main.py).
- Заметки по NiceGUI: [docs/Что нужно для перехода на NiceGUI.md](docs/Что нужно для перехода на NiceGUI.md), [docs/Готовые библиотеки и решения для NiceGUI.md](docs/Готовые библиотеки и решения для NiceGUI.md).

Целевая архитектура с NiceGUI
- Бэкенд остается как есть; NiceGUI работает поверх встроенного FastAPI.
- HTTP маршруты для тяжелых операций выносятся в FastAPI под /api.
- UI строится на компонентах NiceGUI и AG-Grid при необходимости.
- Асинхронная модель: операции ввода-вывода через async, UI обновления через @ui.refreshable.
- Состояние на стороне клиента/сессии хранится в моделях и реактивных переменных NiceGUI.

Структура проекта после миграции
```
prodplan/
├── src/
│   ├── ui_nicegui/
│   │   ├── __init__.py
│   │   ├── app.py         # точка входа NiceGUI
│   │   ├── routes.py      # регистрация страниц/маршрутов
│   │   ├── state.py       # модели состояния и реактивы
│   │   ├── components/
│   │   │   ├── tables.py  # таблицы, AG-Grid обертки
│   │   │   ├── filters.py # фильтры, селекты
│   │   │   ├── reports.py # генерация и загрузка
│   │   │   └── layout.py  # shell: меню, шапка, drawer
│   │   └── pages/
│   │       ├── plan.md    # «План производства»
│   │       └── stages.md  # «Этапы»
├── docs/
│   └── План_миграции_на_NiceGUI.md
└── run_ui_nicegui.bat
```

Зависимости
- Добавить: nicegui>=1.4, uvicorn[standard]>=0.30, plotly (опционально), openpyxl (для экспорта), pandas (уже есть).
- Оставить текущие зависимости для бэкенда.
- Обновить [requirements.txt](requirements.txt) в отдельном коммите.

Карта миграции экранов
1. Страница «План производства»
- Отображение плана по изделиям и датам.
- Редактирование дневных планов с автосохранением в production_plan_entries.
- Фильтры по этапам, датам, статусам.
- Экспорт CSV/Excel.
- Кнопки: запуск sync-specs, sync-stock (через API).
2. Страница «Этапы»
- Просмотр планов по выбранному этапу.
- Группировка по изделиям; read-only таблица.
- Пагинация/виртуальный скролл.

Маппинг компонентов Streamlit → NiceGUI
- st.title → ui.label().classes('text-h4 text-weight-medium')
- st.dataframe → ui.aggrid или ui.table.from_pandas
- st.text_input/selectbox → ui.input/ui.select
- sidebar → ui.left_drawer
- button → ui.button

Интеграция с ядром и CLI
- Не вызывать shell-команды из UI; использовать внутренние Python функции модулей.
- Пример: генерация плана использует функции из [src/planner.py](src/planner.py).
- Синхронизация остатков: использовать [src/odata_stock_sync.py](src/odata_stock_sync.py) и/или [src/stock_history.py](src/stock_history.py).
- База данных: подключение через [src/database.py](src/database.py).

API слой (FastAPI в составе NiceGUI)
- Экспонировать POST /api/sync/specs, /api/sync/stock, /api/generate/plan.
- Долгие задачи запускать в фоне с индикацией прогресса (ui.notify/ui.linear_progress).
- Эндпоинты валидируют входные параметры и возвращают JSON со статусом/ошибками.

Состояние и реактивность
- Использовать модели состояния (например, выбранный этап, дата, фильтры).
- Обновление таблиц через методы с декоратором @ui.refreshable.
- Избегать глобальных синглтонов; передавать state в компоненты.

Работа с SQLite
- WAL и индексы уже учтены; избегать долгих write-транзакций.
- Для длительных SELECT использовать отдельные потоки или run_in_executor.
- Короткие INSERT/UPDATE выполнять через to_thread из async-контекста.
- Обернуть доступ к БД в тонкий сервис поверх [src/database.py](src/database.py).

Производительность UI
- Для больших таблиц применять AG-Grid с server-side pagination.
- Виртуальный скролл: props('virtual-scroll') для ui.table.
- Дебаунс ввода поиска: 250–400 мс.
- Кэширование инвариантных данных на время сессии.

Тестирование
- UI тесты: nicegui.testing Screen (основные сценарии редактирования и фильтрации).
- Интеграционные: тестирование эндпоинтов FastAPI.
- Юнит-тесты ядра остаются без изменений.

План работ (2–3 недели)
Спринт 1 (3–4 дня)
- Каркас проекта NiceGUI, базовый layout, роутинг.
- Подключение БД, чтение планов (read-only).
- API заглушки /api/sync/*.
Спринт 2 (4–5 дней)
- Редактирование дневных планов с автосохранением.
- Фильтры и пагинация.
- Экспорт CSV/Excel.
Спринт 3 (4–5 дней)
- Страница «Этапы», оптимизация производительности.
- Прогресс и нотификации длинных операций.
- Финализация, тесты, документация, деплой.

Критерии приемки
- Функциональный паритет с текущим Streamlit UI.
- Операции sync и generate доступны из UI и возвращают понятный статус.
- Тесты UI покрывают основной happy-path и ошибки.
- Производительность таблиц приемлема на объемах 10k+ строк.

Риски и меры
- Блокировки SQLite: короткие транзакции, ретраи при SQLITE_BUSY.
- Большие таблицы: AG-Grid и пагинация.
- Долгие задачи: фоновые потоки/процессы, прогресс и отмена.

Развертывание и запуск
- Dev: запуск через ui.run(reload=True, show=False, port=8080).
- Prod: uvicorn запускает встроенный FastAPI: uvicorn src.ui_nicegui.app:fastapi_app --host 0.0.0.0 --port 8080.
- Windows .bat: обновить run_ui.bat или добавить run_ui_nicegui.bat.

Чек-лист миграции
- [ ] Добавлены зависимости.
- [ ] Создан каркас src/ui_nicegui.
- [ ] Реализована страница «План производства».
- [ ] Реализована страница «Этапы».
- [ ] Подключены API для sync/generate.
- [ ] Тесты UI добавлены.
- [ ] Документация обновлена.

Скелет приложения (пример)
```python
from nicegui import ui, app
from fastapi import FastAPI

fastapi_app = FastAPI(title='PRODPLAN API')
app.native.router.mount('/api', fastapi_app)

@fastapi_app.get('/health')
async def health():
    return {'status': 'ok'}

def shell():
    with ui.header().classes('justify-between'):
        ui.label('PRODPLAN').classes('text-h6')
        ui.button('Обновить остатки', on_click=lambda: ui.notify('Запуск sync...'))
    with ui.left_drawer(top_corner=True, bottom_corner=True).classes('w-64'):
        ui.link('План производства', '/plan')
        ui.link('Этапы', '/stages')

@ui.page('/')
def index_page():
    shell()
    ui.link('Перейти к плану', '/plan')

@ui.page('/plan')
def plan_page():
    shell()
    ui.label('План производства').classes('text-h5')
    # TODO: таблица, фильтры, редактирование

@ui.page('/stages')
def stages_page():
    shell()
    ui.label('Этапы').classes('text-h5')
    # TODO: представление по этапам

if __name__ in {'__main__', '__mp_main__'}:
    ui.run(reload=True, show=False, port=8080)
```

Эндпоинты API (черновик)
```python
from pydantic import BaseModel
from fastapi import BackgroundTasks

class SyncReq(BaseModel):
    source: str = 'odata'  # odata|excel
    dry_run: bool = False

@fastapi_app.post('/sync/stock')
async def sync_stock(req: SyncReq, bg: BackgroundTasks):
    # bg.add_task(run_sync_stock, req.source, req.dry_run)
    return {'status': 'accepted'}

@fastapi_app.post('/sync/specs')
async def sync_specs(bg: BackgroundTasks):
    return {'status': 'accepted'}

@fastapi_app.post('/generate/plan')
async def generate_plan(days: int = 30):
    return {'status': 'accepted', 'days': days}
```

Лучшие практики UI
- Комбинировать ui.splitter для компоновки фильтров и таблиц.
- Использовать ui.dialog для форм редактирования.
- Нотификации ошибок через ui.notify(type='negative').

Принципы код-стайла
- Чистая архитектура: страницы вызывают сервисы ядра, без SQL в UI.
- Исключить бизнес-логику из компонентов.
- Типизация и docstring в новых модулях UI.

Материалы
- Официальная документация NiceGUI.
- Внутренние заметки: [docs/Что нужно для перехода на NiceGUI.md](docs/Что нужно для перехода на NiceGUI.md), [docs/Готовые библиотеки и решения для NiceGUI.md](docs/Готовые библиотеки и решения для NiceGUI.md).