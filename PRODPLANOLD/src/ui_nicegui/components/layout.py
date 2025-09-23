# -*- coding: utf-8 -*-
"""
PRODPLAN: Общий каркас (shell) для страниц NiceGUI
- Верхний header с названием и быстрыми действиями
- Левый drawer с навигацией
"""

from nicegui import ui

def _nav_link(label: str, href: str, active: bool = False) -> None:
    classes = 'w-full justify-start'
    if active:
        classes += ' text-primary font-medium'
    with ui.row().classes('w-full'):
        ui.link(label, href).classes(classes)

def shell(active: str = 'home') -> None:
    """Компонует общий layout страницы."""
    with ui.header().classes('justify-between bg-primary'):
        ui.label('PRODPLAN').classes('text-h6 text-white')
        
        with ui.row().classes('gap-2'):
            
            
            ui.button(
                'Обновить остатки',
                on_click=lambda: ui.run_javascript(
                    "fetch('/api/sync/stock-history', {"
                    "  method: 'POST',"
                    "  headers: {'Content-Type': 'application/json'},"
                    "  body: JSON.stringify({dir: 'ostatki', dry_run: false})"
                    "})"
                    ".then(r => r.json())"
                    ".then(j => window.$nicegui.notify(j.message || JSON.stringify(j)))"
                    ".catch(e => window.$nicegui.notify('Ошибка sync-stock-history: ' + e, 'negative'))"
                ),
            ).props('flat color=white')
            
            ui.button(
                'Обновить спецификации',
                on_click=lambda: ui.run_javascript(
                    "fetch('/api/sync/specs', {"
                    "  method: 'POST',"
                    "  headers: {'Content-Type': 'application/json'},"
                    "  body: JSON.stringify({path: 'specs'})"
                    "})"
                    ".then(r => r.json())"
                    ".then(j => window.$nicegui.notify(j.message || JSON.stringify(j)))"
                    ".catch(e => window.$nicegui.notify('Ошибка sync-specs: ' + e, 'negative'))"
                ),
            ).props('flat color=white')
            
            ui.button(
                'Сгенерировать план',
                on_click=lambda: ui.run_javascript(
                    "fetch('/api/generate/plan', {"
                    "  method: 'POST',"
                    "  headers: {'Content-Type': 'application/json'},"
                    "  body: JSON.stringify({days: 30})"
                    "})"
                    ".then(r => r.json())"
                    ".then(j => window.$nicegui.notify(j.message || JSON.stringify(j)))"
                    ".catch(e => window.$nicegui.notify('Ошибка generate-plan: ' + e, 'negative'))"
                ),
            ).props('flat color=white')

    with ui.left_drawer(top_corner=True, bottom_corner=True).props('width=200'):
        ui.label('Навигация').classes('text-subtitle2 px-2 py-2')
        ui.separator()
        _nav_link('Главная', '/', active == 'home')
        _nav_link('План выпуска техники', '/plan', active == 'plan')
        _nav_link('Этапы', '/stages', active == 'stages')
        _nav_link('Настройки синхронизации 1С', '/settings/odata', active == 'settings')
        ui.separator().classes('my-2')
        ui.label('Операции').classes('text-subtitle2 px-2 py-2')
        with ui.column().classes('px-2 gap-2'):
            ui.button(
                'Проверка API',
                on_click=lambda: ui.run_javascript(
                    "fetch('/api/health')"
                    ".then(r => r.json())"
                    ".then(j => window.$nicegui.notify(JSON.stringify(j)))"
                    ".catch(e => window.$nicegui.notify('Health error: ' + e, 'negative'))"
                ),
            ).props('outline dense color=primary')
            
            ui.button(
                'Обновить остатки',
                on_click=lambda: ui.run_javascript(
                    "fetch('/api/sync/stock-history', {"
                    "  method: 'POST',"
                    "  headers: {'Content-Type': 'application/json'},"
                    "  body: JSON.stringify({dir: 'ostatki', dry_run: false})"
                    "})"
                    ".then(r => r.json())"
                    ".then(j => window.$nicegui.notify(j.message || JSON.stringify(j)))"
                    ".catch(e => window.$nicegui.notify('Ошибка sync-stock-history: ' + e, 'negative'))"
                ),
            ).props('outline dense color=primary')
            
            ui.button(
                'Сгенерировать план',
                on_click=lambda: ui.run_javascript(
                    "fetch('/api/generate/plan', {"
                    "  method: 'POST',"
                    "  headers: {'Content-Type': 'application/json'},"
                    "  body: JSON.stringify({days: 30})"
                    "})"
                    ".then(r => r.json())"
                    ".then(j => window.$nicegui.notify(j.message || JSON.stringify(j)))"
                    ".catch(e => window.$nicegui.notify('Ошибка generate-plan: ' + e, 'negative'))"
                ),
            ).props('outline dense color=primary')

    # Общий контейнер страницы
    ui.space()
