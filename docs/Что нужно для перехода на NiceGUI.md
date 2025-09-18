Что нужно для перехода на NiceGUI
Установка и настройка
1. Установка зависимостей
bash
pip install nicegui
# Опционально для расширенных возможностей
pip install pandas plotly  # если работаете с данными
2. Базовая структура проекта
text
your_project/
├── main.py          # Точка входа NiceGUI
├── components/      # Переиспользуемые компоненты
├── data/           # Ваши SQLite базы
├── api/            # API интеграции (1C, OData)
└── static/         # Статичные файлы
Миграция кода со Streamlit
Основные замены:
Streamlit	NiceGUI	Примечание
st.title()	ui.label().classes('text-h4')	Заголовки
st.text_input()	ui.input()	Поля ввода
st.button()	ui.button()	Кнопки
st.dataframe()	ui.table.from_pandas()	Таблицы
st.selectbox()	ui.select()	Выпадающие списки
st.sidebar	ui.left_drawer()	Боковая панель
Пример миграции поиска по базе:
Было в Streamlit:

python
import streamlit as st
import sqlite3
import pandas as pd

query = st.text_input("Поиск номенклатуры")
if query:
    df = pd.read_sql(f"SELECT * FROM items WHERE name LIKE '%{query}%'", conn)
    st.dataframe(df)
Стало в NiceGUI:

python
from nicegui import ui
import sqlite3
import pandas as pd

class SearchApp:
    def __init__(self):
        self.conn = sqlite3.connect('your_db.db')
        
    @ui.refreshable
    def show_results(self, query=''):
        if query:
            df = pd.read_sql(f"SELECT * FROM items WHERE name LIKE '%{query}%'", self.conn)
            ui.table.from_pandas(df).props('dense')
        
    def setup_ui(self):
        ui.input('Поиск номенклатуры', 
                 on_change=lambda e: self.show_results.refresh(e.value))
        self.show_results()

app = SearchApp()
app.setup_ui()
ui.run()
Адаптация ваших существующих проектов
1. Интеграция с SQLite (ваша специализация)
python
from nicegui import ui
import sqlite3
from contextlib import asynccontextmanager

class DatabaseManager:
    def __init__(self, db_path):
        self.db_path = db_path
        
    @asynccontextmanager
    async def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()
            
    @ui.refreshable
    async def hierarchical_data_view(self, parent_id=None):
        async with self.get_connection() as conn:
            # Ваша логика для иерархических данных
            cursor = conn.execute("SELECT * FROM specifications WHERE parent_id = ?", (parent_id,))
            rows = cursor.fetchall()
            
            for row in rows:
                with ui.expansion(row[1]):  # название
                    ui.label(f"Артикул: {row[2]}")
                    ui.label(f"Количество: {row[3]}")
2. API интеграции (1C/OData)
python
import requests
from nicegui import ui

class ODataIntegration:
    def __init__(self, base_url, auth):
        self.base_url = base_url
        self.auth = auth
        
    @ui.refreshable
    async def fetch_nomenclature(self):
        try:
            response = requests.get(f"{self.base_url}/Nomenclature", auth=self.auth)
            data = response.json()
            
            # Отображение в таблице
            ui.table(
                columns=[{'name': 'code', 'label': 'Код'}, 
                        {'name': 'name', 'label': 'Наименование'}],
                rows=data['value']
            )
        except Exception as e:
            ui.notify(f"Ошибка: {e}", type='negative')
Настройка среды разработки
1. Hot-reload развертывание
python
# main.py
from nicegui import ui

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(reload=True, show=False, port=8080)
2. Структура для production
python
from nicegui import ui, app
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# Интеграция с существующими FastAPI маршрутами
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/api/data")
async def get_data():
    # Ваша логика API
    return {"data": "from your existing endpoints"}
Что учесть при переходе
1. Изменения в архитектуре
Состояние: Не нужно использовать session_state, состояние сохраняется автоматически

Обновления: Используйте @ui.refreshable декоратор вместо st.rerun()

События: Async/await поддержка для лучшей производительности

2. Стилизация
python
# Кастомные стили с Tailwind CSS
ui.label('Заголовок').classes('text-2xl font-bold text-blue-600')
ui.button('Кнопка').classes('bg-green-500 hover:bg-green-700')
3. Тестирование
python
# tests/test_ui.py
from nicegui.testing import Screen

def test_search_functionality():
    with Screen('/') as screen:
        screen.find("Поиск").type("test query")
        screen.should_contain("результат поиска")