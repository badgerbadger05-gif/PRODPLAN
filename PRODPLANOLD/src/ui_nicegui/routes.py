# -*- coding: utf-8 -*-
"""
PRODPLAN: Регистрация страниц NiceGUI
- Страницы: '/', '/plan', '/stages'
- Общий каркас (shell) подключается из components/layout.py
"""

from nicegui import ui
from datetime import date as _date
from .components.layout import shell
from .services.plan_service import (
    fetch_plan_overview,
    fetch_stages,
    upsert_plan_entry,
    query_plan_overview_paginated,
    ensure_item_exists,
    query_plan_matrix_paginated,
    ensure_root_product_by_code,
)
from .services.search_service import search_items_with_index


def register_routes() -> None:
    """Регистрирует страницы приложения."""

    @ui.page('/')
    def index_page() -> None:
        shell(active='home')
        with ui.card():
            ui.label('Добро пожаловать в PRODPLAN (NiceGUI)').classes('text-h5')
            ui.label('Начните работу со страницы "План выпуска техники"')
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
        # Панель операций на странице плана убрана (кнопки доступны в header/drawer)

        # Состояние страницы
        state = {
            'start': _date.today().isoformat(),
            'days': 21,
            'stage_id': 0,          # 0 == все этапы
            'limit': 200,           # для совместимости; не используется в server-side
            'page': 1,
            'page_size': 30,
            'total': 0,
            'sort_by': 'item_name',
            'sort_dir': 'asc',
            'selected_item_id': None,
        }
        state['search_q'] = ''
        state['search_results'] = []

        # Загрузка глобальной настройки горизонта из config/ui_settings.json
        try:
            from pathlib import Path as _Path
            import json as _json
            _ui_cfg_path = _Path('config') / 'ui_settings.json'
            if _ui_cfg_path.exists():
                _cfg = _json.loads(_ui_cfg_path.read_text('utf-8') or '{}')
                if isinstance(_cfg, dict) and 'plan_horizon_days' in _cfg:
                    state['days'] = int(_cfg.get('plan_horizon_days') or state['days'])
        except Exception:
            pass

        def _run_search():
            q = (state.get('search_q') or '').strip()
            try:
                state['search_results'] = search_items_with_index(q, limit=10) if len(q) >= 2 else []
            except Exception as e:
                ui.notify(f'Ошибка поиска: {e}', type='negative')
                state['search_results'] = []
            render_search_results.refresh()

        def _add_item_to_plan(rec: dict):
            try:
                code = str(rec.get('item_code') or '')
                name = str(rec.get('item_name') or '')
                article = str(rec.get('item_article') or '') or None
                if not code:
                    ui.notify('Не удалось определить код изделия', type='warning'); return
                # Гарантируем наличие items и строки плана (root_products), как это делал Streamlit
                ensure_root_product_by_code(item_code=code, item_name=name, item_article=article)
                ui.notify(f'Добавлено: {name or code}', type='positive')
                render_table.refresh()
            except Exception as e:
                ui.notify(f'Ошибка добавления: {e}', type='negative')

        @ui.refreshable
        def render_search_results():
            results = state.get('search_results') or []
            with ui.column().classes('w-full gap-1'):
                if not results:
                    return
                for r in results:
                    name = str(r.get('item_name') or '')
                    code = str(r.get('item_code') or '')
                    article = str(r.get('item_article') or '')
                    with ui.row().classes('items-center gap-2 w-full'):
                        ui.label(name or '—').classes('flex-1')
                        ui.label(article if article.strip() else '—').classes('text-caption')
                        ui.label(code).classes('text-caption text-grey-7')
                        ui.button('Добавить', on_click=lambda _e, rec=r: _add_item_to_plan(rec)).props('dense color=primary outline')

        # Диалог «Добавить изделие» с автокомплитом (строковый + семантический фолбэк по индексу)
        add_item_dlg = ui.dialog()
        with add_item_dlg:
            with ui.card().classes('w-[720px] max-w-full'):
                ui.label('Добавить изделие в план').classes('text-h6 mb-2')
                add_search_input = ui.input('Поиск по наименованию / артикулу / коду').props('dense clearable').classes('w-full')
                results_box = ui.column().classes('w-full gap-1 max-h-[50vh] overflow-auto')

                def _render_add_results():
                    results_box.clear()
                    results = state.get('search_results') or []
                    if not results:
                        return
                    for r in results:
                        name = str(r.get('item_name') or '')
                        code = str(r.get('item_code') or '')
                        article = str(r.get('item_article') or '')
                        with ui.row().classes('items-center gap-2 w-full'):
                            ui.label(name or '—').classes('flex-1')
                            ui.label(article if article.strip() else '—').classes('text-caption')
                            ui.label(code).classes('text-caption text-grey-7')
                            ui.button('Добавить',
                                      on_click=lambda _e, rec=r: (_add_item_to_plan(rec), add_item_dlg.close())
                                      ).props('dense color=primary outline')

                def _on_add_query_change(e):
                    try:
                        new_q = str(getattr(e, 'value', '') or getattr(e, 'args', '') or (add_search_input.value or ''))
                        state['search_q'] = new_q
                        state['search_results'] = search_items_with_index(new_q, limit=20) if len(new_q) >= 2 else []
                        _render_add_results()
                    except Exception as _e:
                        ui.notify(f'Ошибка поиска: {_e}', type='negative')
                        state['search_results'] = []
                        _render_add_results()

                add_search_input.on('update:model-value', _on_add_query_change)
                with ui.row().classes('justify-end w-full mt-2 gap-2'):
                    ui.button('Закрыть', on_click=add_item_dlg.close).props('outline')

        def _open_add_dialog():
            state['search_q'] = ''
            state['search_results'] = []
            add_item_dlg.open()

        # Обработчик применения горизонта (должен быть определён до использования в верхней панели)
        def _apply_horizon():
            try:
                new_days = int(horizon_input.value or state['days'])
                if new_days < 1:
                    new_days = 1
                state['days'] = new_days
                # Сохраняем глобально в config/ui_settings.json
                from pathlib import Path as _Path
                import json as _json
                p = _Path('config') / 'ui_settings.json'
                p.parent.mkdir(parents=True, exist_ok=True)
                data = {}
                try:
                    if p.exists():
                        data = _json.loads(p.read_text('utf-8') or '{}') or {}
                except Exception:
                    data = {}
                data['plan_horizon_days'] = new_days
                p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                ui.notify('Горизонт обновлён', type='positive')
                render_table.refresh()
            except Exception as e:
                ui.notify(f'Ошибка применения: {e}', type='negative')
        # Открытие диалога добавления по кастомному событию (Enter на последней строке)
        ui.on('open_add_item', lambda _: _open_add_dialog())

        # Панель управления: Добавить / Сохранить изменения
        # Единая панель управления над таблицей (поиск + кнопки в один ряд)
        with ui.row().classes('items-center gap-2 w-full mb-2 flex-nowrap overflow-x-auto'):
            # Поле поиска для быстрого добавления
            top_search_input = ui.input('Номенклатура (поиск: наименование / артикул / код)') \
                                 .props('dense clearable') \
                                 .classes('min-w-[280px] max-w-[420px]')

            def _save_changes():
                js = (
                    "(()=>{ const pending = Array.isArray(window.__pp_pending) ? window.__pp_pending : []; "
                    "console.log('[PP] pending before save', pending);"
                    "if(!pending.length){ window.$nicegui?.notify?.('Нет изменений для сохранения'); return; }"
                    "fetch('/api/plan/bulk_upsert', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({entries: pending})})"
                    ".then(r => (console.log('[PP] bulk_upsert status', r.status), r.json()))"
                    ".then(j => {"
                    " console.log('[PP] bulk_upsert response', j);"
                    " if(j && j.status==='ok'){ window.$nicegui?.notify?.('Сохранено записей: ' + (j.saved||0)); window.__pp_pending = []; window.dispatchEvent(new CustomEvent('plan_saved')); }"
                    " else { window.$nicegui?.notify?.('Ошибка сохранения: ' + (j && j.message ? j.message : 'unknown'), 'negative'); }"
                    "})"
                    ".catch(e => { console.error('[PP] bulk_upsert error', e); window.$nicegui?.notify?.('Ошибка сохранения: ' + e, 'negative'); }); })()"
                )
                ui.run_javascript(js)

            def _open_add_from_top():
                try:
                    q = str(top_search_input.value or '').strip()
                    state['search_q'] = q
                    # префилл поля диалога и вычисление результатов
                    add_search_input.value = q
                    try:
                        state['search_results'] = search_items_with_index(q, limit=20) if len(q) >= 2 else []
                    except Exception:
                        state['search_results'] = []
                    # перерисовать список результатов в диалоге
                    try:
                        # _render_add_results определён выше, внутри диалога
                        # вызовем через его замыкание
                        # если по какой-то причине отсутствует, просто откроем диалог
                        pass
                    finally:
                        add_item_dlg.open()
                except Exception:
                    add_item_dlg.open()

            # Enter в поле поиска открывает диалог «Добавить»
            top_search_input.on('keydown.enter', lambda e: _open_add_from_top())

            ui.button('Добавить', on_click=_open_add_from_top).props('color=primary')
            ui.button('Сохранить изменения', on_click=_save_changes).props('color=positive')

            # Горизонт дат
            horizon_input = ui.number('Горизонт, дней', value=state['days'], min=1, max=90, step=1) \
                               .props('dense') \
                               .classes('w-[150px]')
            ui.button('Применить', on_click=_apply_horizon).props('outline')

            # Удаление выбранной строки


        # Перерисовать таблицу после успешного сохранения
        ui.on('plan_saved', lambda _: render_table.refresh())

        # Управление горизонтом (глобальная настройка)
        def _apply_horizon():
            try:
                new_days = int(horizon_input.value or state['days'])
                if new_days < 1:
                    new_days = 1
                state['days'] = new_days
                # Сохраняем глобально в config/ui_settings.json
                from pathlib import Path as _Path
                import json as _json
                p = _Path('config') / 'ui_settings.json'
                p.parent.mkdir(parents=True, exist_ok=True)
                data = {}
                try:
                    if p.exists():
                        data = _json.loads(p.read_text('utf-8') or '{}') or {}
                except Exception:
                    data = {}
                data['plan_horizon_days'] = new_days
                p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                ui.notify('Горизонт обновлён', type='positive')
                render_table.refresh()
            except Exception as e:
                ui.notify(f'Ошибка применения: {e}', type='negative')

        # (панель объединена выше в единый ряд)


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

        # Вынести за пределы функции!
        grid_container = ui.column().classes('w-full')

        @ui.refreshable
        def render_table() -> None:
            import datetime as _dt
            # Загружаем матрицу плана по дням (левый столбец — сегодня)
            try:
                # Самый левый день всегда текущий: перед каждым рендером фиксируем старт = сегодня
                state['start'] = _date.today().isoformat()
                stage_id = None if state['stage_id'] in (0, None, '') else int(state['stage_id'])
                data = query_plan_matrix_paginated(
                    start_date_str=state['start'],
                    days=int(state['days']),
                    stage_id=stage_id,
                    page=int(state['page']),
                    page_size=int(state['page_size']),
                    sort_by=state['sort_by'],
                    sort_dir=state['sort_dir'],
                )
                rows = data.get('rows', [])
                dates = [str(d) for d in (data.get('dates') or [])]
                state['total'] = int(data.get('total', 0))
                # Трансформируем rows в плоский вид полей по дням (чтобы AG‑Grid редактировал field, а не valueGetter)
                rows2 = []
                try:
                    for r in (rows or []):
                        r2 = dict(r)
                        r_days = dict(r.get('days') or {})
                        for ds in dates:
                            try:
                                r2[f"d_{ds}"] = int(r_days.get(ds, 0))
                            except Exception:
                                r2[f"d_{ds}"] = 0
                        rows2.append(r2)
                except Exception:
                    rows2 = rows
            except Exception as e:
                rows = []
                dates = []
                ui.notify(f'Ошибка загрузки плана: {e}', type='negative')

            # CSS для выходных/праздников
            ui.add_head_html('''
            <style>
              .weekend-col .ag-header-cell-label { background: #f2f2f2 !important; }
              .weekend-cell { background: #fafafa !important; }
              .holiday-col .ag-header-cell-label { background: #e8f0fe !important; }
              .holiday-cell { background: #f3f8ff !important; }
            </style>
            ''')

            # Определяем выходные и праздники РФ
            weekends = set()
            holidays_ru = set()
            try:
                import holidays as _hol
                years = set()
                for ds in dates:
                    try:
                        d = _dt.date.fromisoformat(ds); years.add(d.year)
                    except Exception:
                        pass
                ru = _hol.country_holidays('RU', years=sorted(years)) if years else set()
            except Exception:
                ru = set()

            for ds in dates:
                try:
                    d = _dt.date.fromisoformat(ds)
                    if d.weekday() >= 5:
                        weekends.add(ds)
                    if ru and d in ru:
                        holidays_ru.add(ds)
                except Exception:
                    pass

            # Базовые колонки
            column_defs = [
                {'headerName': 'Изделие', 'field': 'item_name', 'pinned': 'left', 'minWidth': 200},
                {'headerName': 'Артикул', 'field': 'item_article', 'minWidth': 120},
                {'headerName': 'Код', 'field': 'item_code', 'minWidth': 120, 'hide': True},
                {'headerName': 'План на месяц', 'field': 'month_plan', 'type': 'rightAligned', 'minWidth': 120},
                {'headerName': 'ID', 'field': 'item_id', 'hide': True},
            ]

            # Динамические колонки по дням
            js_stage = 'null' if stage_id is None else str(stage_id)
            for ds in dates:
                try:
                    d = _dt.date.fromisoformat(ds); header = d.strftime('%d.%m')
                except Exception:
                    header = ds
                is_weekend = ds in weekends
                is_holiday = ds in holidays_ru
                header_class = 'holiday-col' if is_holiday else ('weekend-col' if is_weekend else '')
                cell_class = 'holiday-cell' if is_holiday else ('weekend-cell' if is_weekend else '')
                # Поле данных для редактирования этой даты: d_YYYY-MM-DD (заполнено в rows2)
                field_key = f"d_{ds}"
                value_setter = (
                    "function(params){ "
                    "  const nv = Number(params.newValue) || 0; "
                    "  if(!Number.isFinite(nv) || nv < 0) return false; "
                    f"  params.data['d_{ds}'] = Math.floor(nv); "
                    "  try { let sum = 0; for(const k in params.data){ if(k && k.startsWith('d_')) sum += Number(params.data[k]||0); } params.data['month_plan'] = sum; } catch(_e) {} "
                    f"  try{{ window.__pp_add_change && window.__pp_add_change(params.data.item_id, '{ds}', Math.floor(nv)); }}catch(__e){{}} "
                    "  return true; "
                    "}"
                )
                column_defs.append({
                    'headerName': header,
                    'colId': ds,                 # идентификатор дня для логики сохранения
                    'field': field_key,          # редактируемое поле данных
                    'type': 'rightAligned',
                    'editable': True,
                    'cellEditor': 'agNumberCellEditor',
                    'cellEditorParams': {'min': 0, 'precision': 0, 'step': 1},
                    'valueSetter': value_setter,
                    'valueFormatter': "params.value != null ? String(params.value) : '0'",
                    'headerClass': header_class,
                    'cellClass': cell_class,
                })

            total_pages = max(1, (state['total'] + state['page_size'] - 1) // state['page_size'])

            # Очищаем контейнер перед перерисовкой
            grid_container.clear()
            
            with grid_container:
                grid_options = {
                    'columnDefs': column_defs,
                    'rowData': rows2,
                    'defaultColDef': {
                        'resizable': True,
                    },
                    'autoSizeStrategy': {
                        'type': 'fitCellContents',
                        'skipHeader': False,
                    },
                    'rowHeight': 26,
                    'suppressColumnVirtualisation': False,
                    'singleClickEdit': True,
                    'enterNavigatesVertically': True,
                    'enterNavigatesVerticallyAfterEdit': True,
                    'stopEditingWhenCellsLoseFocus': True,
                    'suppressClickEdit': False,
                    'undoRedoCellEditing': True,
                    'enableCellTextSelection': True,
                    'rowSelection': 'single',
                }
                
                # Авторазмер с фолбэком (единый обработчик) и вспомогательные события
                grid_options['onFirstDataRendered'] = (
                    f"(p)=>{{ "
                    f" try{{ window.__pp_pending = []; window.__pp_stage = {js_stage}; }}catch(e){{}} "
                    " setTimeout(()=>{ try{ "
                    "  if(p.columnApi && p.columnApi.autoSizeAllColumns){ p.columnApi.autoSizeAllColumns(); } "
                    "  else if(p.api && p.api.sizeColumnsToFit){ p.api.sizeColumnsToFit(); } "
                    " }catch(e){} }, 0); "
                    " try{ window.__pp_add_change = function(item_id, date, qty){ "
                    "   try{ if(!Array.isArray(window.__pp_pending)) window.__pp_pending = []; "
                    "       const key = String(item_id)+'|'+String(date)+'|'+String(window.__pp_stage??'null'); "
                    "       let found=false; "
                    "       for(let i=0;i<window.__pp_pending.length;i++){ const e=window.__pp_pending[i]; "
                    "           const ek = String(e.item_id)+'|'+String(e.date)+'|'+String(e.stage_id??'null'); "
                    "           if(ek===key){ window.__pp_pending[i].qty = qty; found=true; break; } } "
                    "       if(!found){ window.__pp_pending.push({item_id:item_id, date:date, qty:qty, stage_id:(window.__pp_stage??null)}); } "
                    "   }catch(err){} "
                    " }; }catch(e){} "
                    "}"
                )
                grid_options['onGridSizeChanged'] = "(p)=>{}"
                grid_options['onCellClicked'] = (
                    "(e)=>{ try{ const id = e && e.colDef && e.colDef.colId; "
                    "if(id && /^\\d{4}-\\d{2}-\\d{2}$/.test(String(id))){ "
                    " e.api.startEditingCell({ rowIndex: e.node.rowIndex, colKey: 'd_'+id }); } }catch(err){} }"
                )
                grid_options['onGridReady'] = "(p)=>{}"
                grid_options['onCellEditingStarted'] = "(e)=>console.log('Edit started:', e?.column?.colId || e?.column?.getColId?.(), 'value:', e?.value)"
                grid_options['onCellEditingStopped']  = "(e)=>console.log('Edit stopped:',  e?.column?.colId || e?.column?.getColId?.(), 'new:', e?.newValue)"
                # Логирование и дублирование добавления в буфер изменений
                grid_options['onCellValueChanged'] = (
                    "(e)=>{ try{ const colId = e && e.colDef && e.colDef.colId; const field = e && e.colDef && e.colDef.field; "
                    " console.log('[PP] cellValueChanged', {colId, field, item_id: e?.data?.item_id, old: e.oldValue, new: e.newValue}); "
                    " if(colId && /^\\d{4}-\\d{2}-\\d{2}$/.test(String(colId))){ "
                    "   const newQty = Number(e.newValue||0); "
                    "   if(!Number.isFinite(newQty) || newQty < 0){ return; } "
                    "   if(!Array.isArray(window.__pp_pending)) window.__pp_pending = []; "
                    "   const stage = (window.__pp_stage ?? null); "
                    "   const key = String(e.data.item_id)+'|'+String(colId)+'|'+String(stage??'null'); "
                    "   let found=false; "
                    "   for(let i=0;i<window.__pp_pending.length;i++){ const x=window.__pp_pending[i]; "
                    "     const k = String(x.item_id)+'|'+String(x.date)+'|'+String(x.stage_id??'null'); "
                    "     if(k===key){ window.__pp_pending[i].qty = Math.floor(newQty); found=true; break; } "
                    "   } "
                    "   if(!found){ window.__pp_pending.push({item_id:e.data.item_id, date:String(colId), qty:Math.floor(newQty), stage_id:stage}); } "
                    "   console.log('[PP] pending updated', window.__pp_pending); "
                    " } "
                    "}catch(ex){ console.error('[PP] onCellValueChanged error', ex);} }"
                )

                # onGridReady не используется: инициализация перенесена в onFirstDataRendered (стабильнее для NiceGUI wrapper)
                grid_options['onCellKeyDown'] = (
                    "(e)=>{ try{ if(e.event && e.event.key==='Enter'){ "
                    "  const last = e.api.getDisplayedRowCount()-1; "
                    "  if(e.node && e.node.rowIndex === last){ "
                    "    window.dispatchEvent(new CustomEvent('open_add_item')); "
                    "    e.event.preventDefault(); e.event.stopPropagation(); "
                    "  } } }catch(err){} }"
                )
                
                grid = ui.aggrid(grid_options, theme='alpine').style('width: 100%; height: 70vh;')

                async def _on_selection_changed(_):
                    try:
                        selected = await grid.get_selected_rows()
                    except Exception:
                        selected = []
                    try:
                        state['selected_item_id'] = int(selected[0].get('item_id')) if (selected and isinstance(selected[0], dict)) else None
                    except Exception:
                        state['selected_item_id'] = None

                grid.on('selectionChanged', _on_selection_changed)
                
                # Проверка версии AG Grid
                ui.run_javascript(
                    "console.log('AG Grid version:', window.agGrid?.VERSION || window.agGrid?.version || 'unknown');"
                )

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
                render_table()
                # Авто-рефреш отключён: таблица статична до явных действий (Сохранить/Добавить/Применить)


                # Отступ перед таблицей
                ui.row().classes('mt-2')

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

    # Блок синхронизации номенклатуры
    with ui.card().classes('w-full max-w-2xl mt-4'):
        ui.label('Синхронизация номенклатуры').classes('text-h6 mb-2')
        
        # Загрузка текущих настроек синхронизации
        try:
            from pathlib import Path as _Path
            import json as _json
            _sync_cfg_path = _Path('config') / 'nomenclature_sync_config.json'
            _sync_cfg = _json.loads(_sync_cfg_path.read_text('utf-8')) if _sync_cfg_path.exists() else {}
        except Exception:
            _sync_cfg = {}
        
        with ui.column().classes('gap-2'):
            # Поля ввода для периодичности и времени старта
            interval_input = ui.input('Периодичность синхронизации (часы)',
                                      value=str(_sync_cfg.get('interval_hours', 1))).props('type=number min=1 dense')
            time_input = ui.input('Время старта синхронизации (Ч:ММ)',
                                  value=str(_sync_cfg.get('start_time', '09:00'))).props('type=time dense')
            
            # Прогресс бар для синхронизации
            # Диалог с нативным HTML progress для устойчивого обновления из JS
            progress_dlg_sync = ui.dialog()
            with progress_dlg_sync:
                with ui.card():
                    ui.label('Синхронизация номенклатуры…')
                    ui.html('<progress id="nom_sync_prog" max="100" value="0" style="width: 400px;"></progress>')
                    ui.html('<div id="nom_sync_lbl" class="text-caption">0%</div>')
            # Закрытие диалога после завершения (через custom event из JS)
            ui.on('close_sync_progress', lambda _: progress_dlg_sync.close())
            
            def _save_sync_settings():
                try:
                    from pathlib import Path as _Path
                    import json as _json
                    _p = _Path('config') / 'nomenclature_sync_config.json'
                    _p.parent.mkdir(parents=True, exist_ok=True)
                    _data = {
                        'interval_hours': int(interval_input.value or 1),
                        'start_time': str(time_input.value or '09:0'),
                    }
                    _p.write_text(_json.dumps(_data, ensure_ascii=False, indent=2), encoding='utf-8')
                    ui.notify('Настройки синхронизации сохранены', type='positive')
                except Exception as e:
                    ui.notify(f'Ошибка сохранения настроек: {e}', type='negative')
            
            def _start_sync_now():
                try:
                    # Открываем диалог и запускаем процесс + опрос статуса
                    progress_dlg_sync.open()
                    js = (
                        "(() => {"
                        " const p = document.getElementById('nom_sync_prog');"
                        "  const l = document.getElementById('nom_sync_lbl');"
                        "  if (p) p.value = 0; if (l) l.textContent = 'Начало синхронизации...';"
                        "  return fetch('/api/nomenclature/sync', {"
                        "    method: 'POST',"
                        "    headers: {'Content-Type': 'application/json'}"
                        "  })"
                        "  .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })"
                        "  .then(_ => {"
                        "    const iv = setInterval(() => {"
                        "      fetch('/api/nomenclature/sync/status')"
                        "      .then(r => r.json())"
                        "      .then(status => {"
                        "        const v = Math.max(0, Math.min(100, Number(status.progress || 0)));"
                        "        if (p) p.value = v;"
                        "        if (l) l.textContent = status.message || (v + '%');"
                        "        if (status && status.running === false && (v === 0 || String(status.message||').includes('завершена'))) {"
                        "          clearInterval(iv);"
                        "          window.dispatchEvent(new CustomEvent('close_sync_progress'));"
                        "        }"
                        "      })"
                        "      .catch(e => {"
                        "        console.error('sync status error', e);"
                        "        clearInterval(iv);"
                        "        window.$nicegui?.notify?.('Ошибка статуса синхронизации: ' + e, 'negative');"
                        "        window.dispatchEvent(new CustomEvent('close_sync_progress'));"
                        "      });"
                        "    }, 500);"
                        "  })"
                        "  .catch(e => {"
                        "    window.$nicegui?.notify?.('Ошибка запуска синхронизации: ' + e, 'negative');"
                        "    window.dispatchEvent(new CustomEvent('close_sync_progress'));"
                        "  });"
                        "})()"
                    )
                    ui.run_javascript(js)
                except Exception as e:
                    ui.notify(f'Ошибка синхронизации: {e}', type='negative')
            
            # Кнопки
            with ui.row().classes('gap-2'):
                ui.button('Начать синхронизацию', on_click=_start_sync_now).props('color=primary')
                ui.button('Сохранить', on_click=_save_sync_settings).props('outline')

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
