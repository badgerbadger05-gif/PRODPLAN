# -*- coding: utf-8 -*-
"""
PRODPLAN: Регистрация страниц NiceGUI
- Страницы: '/', '/plan', '/stages'
- Общий каркас (shell) подключается из components/layout.py
"""

from nicegui import ui
from datetime import date as _date
from .components.layout import shell
from .services.plan_service import fetch_plan_overview, fetch_stages, upsert_plan_entry, query_plan_overview_paginated


def register_routes() -> None:
    """Регистрирует страницы приложения."""

    @ui.page('/')
    def index_page() -> None:
        shell(active='home')
        with ui.card():
            ui.label('Добро пожаловать в PRODPLAN (NiceGUI)').classes('text-h5')
            ui.label('Начните работу со страницы "План производства"')
            # Панель быстрых операций (дублирует кнопки из хедера/дроуера — для явной видимости)
            with ui.row().classes('gap-2'):
                ui.button(
                    'Проверка API',
                    on_click=lambda: ui.run_javascript(
                        "fetch('/api/health')"
                        ".then(r => r.json())"
                        ".then(j => window.$nicegui.notify(JSON.stringify(j)))"
                        ".catch(e => window.$nicegui.notify('Health error: ' + e, 'negative'))"
                    ),
                ).props('outline color=primary')
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
                ).props('outline color=blue')
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
                ).props('color=positive')
            ui.link('Перейти к плану', '/plan').classes('text-primary')

    @ui.page('/plan')
    def plan_page() -> None:
        shell(active='plan')
        ui.label('План производства').classes('text-h6 mb-2')
        # Панель операций на странице плана убрана (кнопки доступны в header/drawer)

        # Состояние страницы
        state = {
            'start': _date.today().isoformat(),
            'days': 30,
            'stage_id': 0,          # 0 == все этапы
            'limit': 200,           # для совместимости; не используется в server-side
            'page': 1,
            'page_size': 50,
            'total': 0,
            'sort_by': 'item_name',
            'sort_dir': 'asc',
            'selected_item_id': None,
        }

        def _apply_filters():
            state['page'] = 1
            render_table.refresh()

        def _set_page(p: int):
            state['page'] = int(max(1, p))
            render_table.refresh()

        def _export(fmt: str):
            stage_id = None if state['stage_id'] in (0, None, '') else int(state['stage_id'])
            qs = f"format={fmt}&start_date={state['start']}&days={int(state['days'])}"
            if stage_id is not None:
                qs += f"&stage_id={stage_id}"
            ui.run_javascript(f"window.open('/api/plan/export?{qs}', '_blank')")

        # Справочник этапов
        stage_map = {0: 'Все этапы'}
        try:
            _stages = fetch_stages()
            stage_map.update({int(s['value']): str(s['label']) for s in _stages})
        except Exception as e:
            ui.notify(f'Ошибка загрузки этапов: {e}', type='warning')

        @ui.refreshable
        def render_table() -> None:
            try:
                stage_id = None if state['stage_id'] in (0, None, '') else int(state['stage_id'])
                data = query_plan_overview_paginated(
                    start_date_str=state['start'],
                    days=int(state['days']),
                    stage_id=stage_id,
                    page=int(state['page']),
                    page_size=int(state['page_size']),
                    sort_by=state['sort_by'],
                    sort_dir=state['sort_dir'],
                )
                rows = data.get('rows', [])
                state['total'] = int(data.get('total', 0))
            except Exception as e:
                rows = []
                ui.notify(f'Ошибка загрузки плана: {e}', type="negative")

            total_pages = max(1, (state['total'] + state['page_size'] - 1) // state['page_size'])

            # Конфигурация AG-Grid (server-side пагинация на уровне приложения)
            grid_options = {
                'columnDefs': [
                    {'headerName': 'Изделие', 'field': 'item_name'},
                    {'headerName': 'Артикул', 'field': 'item_code'},
                    {
                        'headerName': 'План на месяц',
                        'field': 'month_plan',
                        'type': 'rightAligned',
                        'valueFormatter': "params.value != null ? params.value.toLocaleString() : ''",
                    },
                    {'headerName': 'ID', 'field': 'item_id', 'hide': True},
                ],
                'rowData': rows,
                'rowSelection': 'single',
                'pagination': False,
                'animateRows': True,
                'defaultColDef': {
                    'sortable': False,
                    'filter': False,
                    'resizable': True,
                    'minWidth': 120,
                    'flex': 1,
                },
            }
            grid = ui.aggrid(grid_options, theme='alpine').classes('w-full h-[70vh]')

            def _on_selection_changed(_):
                try:
                    selected = grid.get_selected_rows()
                except Exception:
                    selected = []
                state['selected_item_id'] = selected[0]['item_id'] if selected else None

            grid.on('selectionChanged', _on_selection_changed)

            # Пагинация и статус
            with ui.row().classes('items-center justify-between w-full mt-2'):
                ui.label(f"Страница {state['page']} из {total_pages} • Всего записей: {state['total']}")
                with ui.row().classes('gap-2'):
                    ui.button('⏮', on_click=lambda: _set_page(1)).props('dense outline')
                    ui.button('◀', on_click=lambda: _set_page(max(1, state['page'] - 1))).props('dense outline')
                    ui.button('▶', on_click=lambda: _set_page(min(total_pages, state['page'] + 1))).props('dense outline')
                    ui.button('⏭', on_click=lambda: _set_page(total_pages)).props('dense outline')

        # Макет страницы: таблица и редактор
        with ui.row().classes('w-full items-start gap-4'):
            # Блок фильтров удален (ранее: левая колонка с фильтрами)

            # Правая колонка: таблица и действия
            with ui.column().classes('flex-1'):
                with ui.card().classes('w-full'):
                    ui.label('Таблица плана').classes('text-subtitle2')
                    render_table()

                # Редактирование дневной записи плана
                with ui.card().classes('w-full mt-2'):
                    ui.label('Редактирование дневного плана').classes('text-subtitle2')
                    edit_row = ui.row().classes('items-end gap-2')
                    with edit_row:
                        # Редактирование даты через input type=date для устойчивости
                        edit_date = ui.input('Дата', value=state['start']).props('type=date dense')
                        edit_qty = ui.number('Количество', value=0, min=0, step=1).props('dense')
                        def _save():
                            if not state['selected_item_id']:
                                ui.notify('Выберите изделие в таблице', type='warning'); return
                            try:
                                stage_id = None if state['stage_id'] in (0, None, '') else int(state['stage_id'])
                                upsert_plan_entry(
                                    item_id=int(state['selected_item_id']),
                                    date_str=edit_date.value or state['start'],
                                    planned_qty=float(edit_qty.value or 0),
                                    stage_id=stage_id,
                                )
                                ui.notify('Сохранено', type='positive')
                                render_table.refresh()
                            except Exception as e:
                                ui.notify(f'Ошибка сохранения: {e}', type='negative')
                        ui.button('Сохранить запись дня', on_click=_save).props('unelevated color=primary')

                with ui.row().classes('mt-2'):
                    ui.button('Экспорт CSV', on_click=lambda: _export('csv'))
                    ui.button('Экспорт Excel', on_click=lambda: _export('excel'))

    @ui.page('/stages')
    def stages_page() -> None:
        shell(active='stages')
        ui.label('Этапы производства').classes('text-h6 mb-2')
        with ui.card():
            ui.label('Представление по этапам (заглушка)')
            with ui.row():
                ui.button('Механообработка')
                ui.button('Сборка')
                ui.button('Закупка')
            ui.separator()
            ui.label('Здесь будет таблица/группировка по этапам')


@ui.page('/settings/odata')
def odata_settings_page() -> None:
    shell(active='settings')
    ui.label('Настройки синхронизации 1С').classes('text-h6 mb-2')

    # Загрузка текущего конфига для предзаполнения полей
    try:
        from pathlib import Path as _Path
        import json as _json
        _cfg_path = _Path('config') / 'odata_config.json'
        _cfg = _json.loads(_cfg_path.read_text('utf-8')) if _cfg_path.exists() else {}
    except Exception:
        _cfg = {}

    with ui.card().classes('w-full max-w-2xl'):
        with ui.column().classes('gap-2'):
            base_input = ui.input('Базовый URL (base_url)', value=str(_cfg.get('base_url', '') or '')).props('dense')
            user_input = ui.input('Имя пользователя (username)', value=str(_cfg.get('username', '') or '')).props('dense')
            pass_input = ui.input('Пароль (password)', value=str(_cfg.get('password', '') or '')).props('type=password dense')

            def _save_cfg():
                try:
                    from pathlib import Path as _Path
                    import json as _json
                    _p = _Path('config') / 'odata_config.json'
                    _p.parent.mkdir(parents=True, exist_ok=True)
                    _base = (base_input.value or '').strip().rstrip('/')
                    if _base.lower().endswith('$metadata'):
                        _base = _base[: -len('$metadata')].rstrip('/')
                    _data = {
                        'base_url': _base,
                        'username': (user_input.value or '').strip(),
                        'password': pass_input.value or '',
                        # сохраняем совместимые поля, если файл уже есть:
                    }
                    try:
                        if _p.exists():
                            old = _json.loads(_p.read_text('utf-8')) or {}
                            # переносим дополнительные поля (например, entity_name, select_fields), если они были
                            for k in ('entity_name', 'select_fields'):
                                if k in old and k not in _data:
                                    _data[k] = old[k]
                    except Exception:
                        pass
                    _p.write_text(_json.dumps(_data, ensure_ascii=False, indent=2), encoding='utf-8')
                    ui.notify('Настройки сохранены в config/odata_config.json', type='positive')
                except Exception as e:
                    ui.notify(f'Ошибка сохранения настроек: {e}', type='negative')

            def _js_escape(s: str) -> str:
                return (str(s or '')).replace('\\', '\\\\').replace("'", "\\'")

            def _test_conn():
                try:
                    from src.odata_client import OData1CClient as _Client
                    base = (base_input.value or '').strip()
                    if base.lower().endswith('$metadata'):
                        base = base[: -len('$metadata')].rstrip('/')
                    client = _Client(
                        base_url=base,
                        username=(user_input.value or '').strip() or None,
                        password=pass_input.value or None,
                        token=None,
                    )
                    resp = client._make_request('$metadata')
                    if isinstance(resp, dict) and '_raw' in resp:
                        raw = str(resp.get('_raw') or '')
                        ui.notify(f'Подключение успешно • $metadata {len(raw.encode("utf-8", "ignore"))} bytes', type='positive')
                    else:
                        import json as _json
                        ui.notify(f'Подключение успешно • JSON ({len(_json.dumps(resp, ensure_ascii=False))} bytes)', type='positive')
                except Exception as e:
                    ui.notify(f'Ошибка теста подключения: {e}', type='negative')

            def _fetch_metadata():
                try:
                    from src.odata_client import OData1CClient as _Client
                    from pathlib import Path as _Path
                    import json as _json
                    base = (base_input.value or '').strip()
                    if base.lower().endswith('$metadata'):
                        base = base[: -len('$metadata')].rstrip('/')
                    client = _Client(
                        base_url=base,
                        username=(user_input.value or '').strip() or None,
                        password=pass_input.value or None,
                        token=None,
                    )
                    resp = client._make_request('$metadata')
                    if isinstance(resp, dict) and '_raw' in resp:
                        xml_text = str(resp.get('_raw') or '')
                    else:
                        xml_text = f'<!-- non-XML response -->\n{_json.dumps(resp, ensure_ascii=False, indent=2)}'
                    out_xml = _Path('output') / 'odata_metadata.xml'
                    out_sum = _Path('output') / 'odata_metadata_summary.json'
                    out_xml.parent.mkdir(parents=True, exist_ok=True)
                    out_xml.write_text(xml_text, encoding='utf-8')
                    # simple summary
                    summary = {"entities": [], "entity_sets": []}
                    try:
                        for line in xml_text.splitlines():
                            s = line.strip()
                            if 'EntitySet Name=' in s and 'EntityType=' in s:
                                i = s.find('Name="') + 6; j = s.find('"', i)
                                if i > 5 and j > i:
                                    summary["entity_sets"].append(s[i:j])
                            elif '<EntityType Name=' in s:
                                i = s.find('Name="') + 6; j = s.find('"', i)
                                if i > 5 and j > i:
                                    summary["entities"].append(s[i:j])
                    except Exception:
                        pass
                    out_sum.write_text(_json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
                    ui.notify(f'Метаданные выгружены • XML: {out_xml} • EntitySets: {len(summary.get("entity_sets", []))}', type='positive')
                except Exception as e:
                    ui.notify(f'Ошибка выгрузки метаданных: {e}', type='negative')

            def _force_reindex():
                ui.run_javascript(
                    "fetch('/api/odata/reindex', {method:'POST', headers:{'Content-Type':'application/json'}})"
                    ".then(r => r.json())"
                    ".then(j => window.$nicegui.notify(j.message || JSON.stringify(j)))"
                    ".catch(e => window.$nicegui.notify('Ошибка запуска переиндексации: ' + e, 'negative'))"
                )

            # Диалог прогресса и обработчик для выгрузки групп номенклатуры (IsFolder=true)
            progress_dlg = ui.dialog()
            with progress_dlg:
                with ui.card():
                    ui.label('Выгрузка групп номенклатуры…')
                    ui.html('<progress id="odata_exp_prog" max="100" value="0" style="width: 400px;"></progress>')
                    ui.html('<div id="odata_exp_lbl" class="text-caption">0%</div>')

            def _export_groups():
                try:
                    progress_dlg.open()
                    js = (
                        "(() => {"
                        "  const p = document.getElementById('odata_exp_prog');"
                        "  const l = document.getElementById('odata_exp_lbl');"
                        "  if (p) p.value = 5; if (l) l.textContent = '5%';"
                        "  if (p) p.value = 15; if (l) l.textContent = '15%';"
                        "  return fetch('/api/odata/categories/export_groups', {"
                        "    method: 'POST',"
                        "    headers: {'Content-Type': 'application/json'},"
                        "    body: JSON.stringify({})"
                        "  })"
                        "  .then(r => {"
                        "    if (p) p.value = 60; if (l) l.textContent = '60%';"
                        "    if (!r.ok) throw new Error('HTTP ' + r.status);"
                        "    return r.json();"
                        "  })"
                        "  .then(j => {"
                        "    if (p) p.value = 90; if (l) l.textContent = '90%';"
                        "    window.$nicegui.notify((j.message || 'Готово') + ' • всего: ' + (j.total || 0));"
                        "  })"
                        "  .catch(e => window.$nicegui.notify('Ошибка выгрузки групп: ' + e, 'negative'))"
                        "  .finally(() => {"
                        "    if (p) p.value = 100; if (l) l.textContent = '100%';"
                        "    window.dispatchEvent(new CustomEvent('close_progress'));"
                        "    setTimeout(() => window.dispatchEvent(new CustomEvent('close_progress')), 150);"
                        "  });"
                        "})()"
                    )
                    ui.run_javascript(js)
                except Exception as e:
                    ui.notify(f'Ошибка запуска выгрузки: {e}', type='negative')

            # Закрытие диалога после завершения JS-фетча (через custom event)
            ui.on('close_progress', lambda _: progress_dlg.close())

            with ui.row().classes('gap-2'):
                ui.button('Сохранить настройки', on_click=_save_cfg).props('outline')
                ui.button('Тест подключения', on_click=_test_conn).props('color=primary')
                ui.button('Выгрузить метаданные', on_click=_fetch_metadata).props('color=secondary')
                ui.button('Выгрузить группы номенклатуры', on_click=_export_groups).props('color=secondary')
                ui.button('Принудительная индексация номенклатуры', on_click=_force_reindex).props('color=warning')

    # Подсказка по сохранённым путям для метаданных
    with ui.expansion('Где будет сохранён результат выгрузки метаданных?', value=False).classes('mt-2 w-full max-w-2xl'):
        ui.label('XML: output/odata_metadata.xml')
        ui.label('Summary JSON: output/odata_metadata_summary.json')

    # Просмотр сохранённых групп и выбор для индексации
    with ui.expansion('Группы номенклатуры для индексации', value=False).classes('mt-2 w-full max-w-2xl'):
        try:
            from pathlib import Path as _Path
            import json as _json
            _groups_path = _Path('output') / 'odata_groups_nomenclature.json'
            _sel_path = _Path('config') / 'odata_groups_selected.json'
            _groups = []
            if _groups_path.exists():
                _data = _json.loads(_groups_path.read_text('utf-8'))
                _vals = _data.get('value', _data)
                if isinstance(_vals, dict):
                    _vals = [_vals]
                _groups = [
                    {
                        'id': str(g.get('Ref_Key') or ''),
                        'code': str(g.get('Code') or ''),
                        'name': str(g.get('Description') or ''),
                    }
                    for g in _vals
                    if isinstance(g, dict) and (g.get('IsFolder') is True)
                ]
                _groups.sort(key=lambda x: (x['code'], x['name']))
            _selected_ids = set()
            if _sel_path.exists():
                try:
                    _selected_ids = set(_json.loads(_sel_path.read_text('utf-8')) or [])
                except Exception:
                    _selected_ids = set()
        except Exception as _e:
            _groups = []
            _selected_ids = set()
            ui.notify(f'Ошибка чтения сохранённых групп: {_e}', type='warning')

        # Статистика и операции
        stats_label = ui.label(f'Всего групп: {len(_groups)} • Выбрано: {len(_selected_ids)}').classes('mb-2')

        def _select_all():
            nonlocal _selected_ids
            _selected_ids = {g['id'] for g in _groups}
            groups_panel.refresh()
            stats_label.text = f'Всего групп: {len(_groups)} • Выбрано: {len(_selected_ids)}'

        def _clear_all():
            nonlocal _selected_ids
            _selected_ids = set()
            groups_panel.refresh()
            stats_label.text = f'Всего групп: {len(_groups)} • Выбрано: {len(_selected_ids)}'

        def _save_selection():
            try:
                _sel_path.parent.mkdir(parents=True, exist_ok=True)
                _sel_path.write_text(_json.dumps(sorted(list(_selected_ids)), ensure_ascii=False, indent=2), encoding='utf-8')
                ui.notify('Выбор групп сохранён: config/odata_groups_selected.json', type='positive')
            except Exception as e:
                ui.notify(f'Ошибка сохранения выбора: {e}', type='negative')

        with ui.row().classes('gap-2 mb-2'):
            ui.button('Выбрать все', on_click=_select_all).props('outline')
            ui.button('Снять все', on_click=_clear_all).props('outline')
            ui.button('Сохранить выбор', on_click=_save_selection).props('color=primary')

        @ui.refreshable
        def groups_panel():
            with ui.column().classes('max-h-80 overflow-auto w-full gap-1'):
                for g in _groups:
                    value = g['id'] in _selected_ids
                    cb = ui.checkbox(f"{g['code']} — {g['name']}", value=value).props('dense')
                    def _on_change(ev, _gid=g['id']):
                        if ev.value:
                            _selected_ids.add(_gid)
                        else:
                            _selected_ids.discard(_gid)
                        stats_label.text = f'Всего групп: {len(_groups)} • Выбрано: {len(_selected_ids)}'
                    cb.on('update:model-value', _on_change)
        groups_panel()
