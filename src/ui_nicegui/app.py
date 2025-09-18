# -*- coding: utf-8 -*-
"""
PRODPLAN: Точка входа NiceGUI

- Экспортирует ASGI приложение для запуска через uvicorn: asgi_app
- Монтирует FastAPI под /api
- Регистрирует страницы UI через register_routes()
- Для разработки: python src/ui_nicegui/app.py
"""

from nicegui import ui, app as ng_app
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
import io
import pandas as pd
from pathlib import Path
import json
import requests
from src.odata_client import OData1CClient
from src.database import init_database
from src.planner import generate_production_plan
from .services.plan_service import query_plan_overview_paginated, fetch_plan_dataset


# FastAPI приложение для API-эндпоинтов (монтируется внутрь NiceGUI)
fastapi_app = FastAPI(title='PRODPLAN API', version='1.6')


@fastapi_app.get('/health')
async def health():
    return {'status': 'ok'}

# ---------------------------
# API моделей запросов/ответов
# ---------------------------
class GeneratePlanReq(BaseModel):
    days: int = 30
    start_date: Optional[str] = None
    out: Optional[str] = 'output/production_plan.xlsx'
    db: Optional[str] = None

class SyncStockHistoryReq(BaseModel):
    dir: Optional[str] = 'ostatki'
    db: Optional[str] = None
    dry_run: bool = False

class SyncSpecsReq(BaseModel):
    # Заглушка до реализации spec_importer
    path: Optional[str] = 'specs'
    db: Optional[str] = None


class PlanQueryReq(BaseModel):
    start_date: Optional[str] = None
    days: int = 30
    stage_id: Optional[int] = None
    page: int = 1
    page_size: int = 50
    sort_by: str = 'item_name'
    sort_dir: str = 'asc'
    db: Optional[str] = None


# Монтируем FastAPI в NiceGUI под /api
# Важно: используем корневое приложение NiceGUI (основано на FastAPI), у которого доступен .mount(...)
ng_app.mount('/api', fastapi_app)

# Идемпотентная инициализация схемы БД (гарантирует наличие таблиц)
init_database()

# ---------------------------
# API эндпоинты фоновых операций
# ---------------------------
@fastapi_app.post('/generate/plan')
async def api_generate_plan(req: GeneratePlanReq, bg: BackgroundTasks):
    def _task():
        try:
            result_path = generate_production_plan(
                db_path=req.db,
                output_path=req.out or 'output/production_plan.xlsx',
                horizon_days=int(req.days or 30),
                start_date=None if not req.start_date else __import__('datetime').date.fromisoformat(req.start_date),
            )
            print(f'[generate/plan] done: {result_path}')
        except Exception as e:
            print(f'[generate/plan] error: {e!r}')
    bg.add_task(_task)
    return {'status': 'accepted', 'message': 'Генерация плана запущена в фоне', 'out': req.out}

@fastapi_app.post('/sync/stock-history')
async def api_sync_stock_history(req: SyncStockHistoryReq, bg: BackgroundTasks):
    def _task():
        try:
            from src.stock_history import sync_stock_with_history
            sync_stock_with_history(stock_path=req.dir, db_path=req.db, dry_run=bool(req.dry_run))
            print(f'[sync/stock-history] done (dir={req.dir}, dry_run={req.dry_run})')
        except Exception as e:
            print(f'[sync/stock-history] error: {e!r}')
    bg.add_task(_task)
    return {'status': 'accepted', 'message': 'Синхронизация остатков (с историей) запущена в фоне', 'dir': req.dir, 'dry_run': req.dry_run}

@fastapi_app.post('/sync/specs')
async def api_sync_specs(req: SyncSpecsReq):
    # Заглушка: модуль импорта спецификаций отсутствует в текущем репозитории.
    return {'status': 'not_implemented', 'message': 'Импорт спецификаций пока не реализован в UI (ожидается модуль spec_importer).'}


@fastapi_app.post('/plan/query')
async def api_plan_query(req: PlanQueryReq):
    data = query_plan_overview_paginated(
        start_date_str=req.start_date or __import__('datetime').date.today().isoformat(),
        days=int(req.days or 30),
        stage_id=req.stage_id,
        page=int(req.page or 1),
        page_size=int(req.page_size or 50),
        sort_by=req.sort_by or 'item_name',
        sort_dir=req.sort_dir or 'asc',
        db_path=req.db,
    )
    return data


@fastapi_app.get('/plan/export')
async def api_plan_export(
    format: str = 'csv',
    start_date: Optional[str] = None,
    days: int = 30,
    stage_id: Optional[int] = None,
    db: Optional[str] = None,
):
    rows = fetch_plan_dataset(
        start_date_str=start_date or __import__('datetime').date.today().isoformat(),
        days=int(days or 30),
        stage_id=stage_id,
        db_path=db,
    )
    df = pd.DataFrame(rows)
    if format.lower() in {'excel', 'xlsx'}:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment; filename="plan_export.xlsx"'},
        )
    else:
        csv_buf = io.StringIO()
        df.to_csv(csv_buf, index=False)
        return StreamingResponse(
            iter([csv_buf.getvalue()]),
            media_type='text/csv; charset=utf-8',
            headers={'Content-Disposition': 'attachment; filename="plan_export.csv"'},
        )


# ---------------------------
# OData settings helpers & API
# ---------------------------

class ODataConfigReq(BaseModel):
    base_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

CONFIG_PATH = Path('config') / 'odata_config.json'
OUTPUT_XML = Path('output') / 'odata_metadata.xml'
OUTPUT_SUMMARY = Path('output') / 'odata_metadata_summary.json'
OUTPUT_GROUPS = Path('output') / 'odata_groups_nomenclature.json'

def _normalize_base_url(u: Optional[str]) -> str:
    s = (u or '').strip().rstrip('/')
    if s.lower().endswith('$metadata'):
        s = s[: -len('$metadata')].rstrip('/')
    return s

def _load_odata_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open('r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {
                        'base_url': str(data.get('base_url') or ''),
                        'username': str(data.get('username') or ''),
                        'password': str(data.get('password') or ''),
                    }
    except Exception:
        pass
    return {'base_url': '', 'username': '', 'password': ''}

def _save_odata_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open('w', encoding='utf-8') as f:
        json.dump({
            'base_url': str(cfg.get('base_url') or ''),
            'username': str(cfg.get('username') or ''),
            'password': str(cfg.get('password') or ''),
            # сохраняем прочие поля как есть, если они были
        }, f, ensure_ascii=False, indent=2)

def _parse_metadata_summary(xml: str) -> dict:
    summary = {
        "entities": [],
        "entity_sets": [],
        "functions": [],
        "actions": [],
    }
    try:
        for line in xml.splitlines():
            s = line.strip()
            if 'EntitySet Name=' in s and 'EntityType=' in s:
                start = s.find('Name="') + 6
                end = s.find('"', start)
                if start > 5 and end > start:
                    summary['entity_sets'].append(s[start:end])
            elif '<EntityType Name=' in s:
                start = s.find('Name="') + 6
                end = s.find('"', start)
                if start > 5 and end > start:
                    summary['entities'].append(s[start:end])
    except Exception:
        pass
    return summary

@fastapi_app.post('/odata/config')
async def api_odata_save_config(req: ODataConfigReq):
    cur = _load_odata_config()
    if req.base_url is not None:
        cur['base_url'] = _normalize_base_url(req.base_url)
    if req.username is not None:
        cur['username'] = req.username or ''
    if req.password is not None:
        cur['password'] = req.password or ''
    _save_odata_config(cur)
    return {'status': 'ok', 'message': 'Конфигурация сохранена', 'path': str(CONFIG_PATH)}

@fastapi_app.post('/odata/test')
async def api_odata_test(req: ODataConfigReq):
    cfg = _load_odata_config()
    base_url = _normalize_base_url(req.base_url or cfg.get('base_url'))
    username = req.username if req.username is not None else cfg.get('username') or None
    password = req.password if req.password is not None else cfg.get('password') or None
    if not base_url:
        return {'status': 'error', 'message': 'Не указан base_url (введите и сохраните настройки).'}

    try:
        client = OData1CClient(base_url=base_url, username=username or None, password=password or None, token=None)
        resp = client._make_request('$metadata')
        if isinstance(resp, dict) and '_raw' in resp:
            raw = str(resp.get('_raw') or '')
            size = len(raw.encode('utf-8', errors='ignore'))
            return {'status': 'ok', 'message': f'Подключение успешно. Получено $metadata ({size} bytes).'}
        else:
            # Некоторые серверы могут вернуть JSON-обертку
            size = len(json.dumps(resp, ensure_ascii=False))
            return {'status': 'ok', 'message': f'Подключение успешно. Ответ разобран как JSON ({size} bytes).'}
    except Exception as e:
        return {'status': 'error', 'message': f'Ошибка подключения: {e}'}

@fastapi_app.post('/odata/metadata')
async def api_odata_metadata(req: ODataConfigReq):
    cfg = _load_odata_config()
    base_url = _normalize_base_url(req.base_url or cfg.get('base_url'))
    username = req.username if req.username is not None else cfg.get('username') or None
    password = req.password if req.password is not None else cfg.get('password') or None
    if not base_url:
        return {'status': 'error', 'message': 'Не указан base_url (введите и сохраните настройки).'}

    try:
        client = OData1CClient(base_url=base_url, username=username or None, password=password or None, token=None)
        resp = client._make_request('$metadata')
        xml_text = None
        if isinstance(resp, dict) and '_raw' in resp:
            xml_text = str(resp.get('_raw') or '')
        else:
            # Если сервер вернул не raw-XML — сериализуем как JSON для диагностики
            xml_text = f'<!-- non-XML response -->\n{json.dumps(resp, ensure_ascii=False, indent=2)}'

        # Сохраняем файлы
        OUTPUT_XML.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT_XML.open('w', encoding='utf-8') as f:
            f.write(xml_text)

        summary = _parse_metadata_summary(xml_text)
        with OUTPUT_SUMMARY.open('w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        return {
            'status': 'ok',
            'message': 'Метаданные выгружены',
            'xml': str(OUTPUT_XML),
            'summary': str(OUTPUT_SUMMARY),
            'entity_sets': len(summary.get('entity_sets', [])),
            'entities': len(summary.get('entities', [])),
        }
    except Exception as e:
        return {'status': 'error', 'message': f'Ошибка выгрузки метаданных: {e}'}

@fastapi_app.post('/odata/categories/export_groups')
async def api_odata_export_groups(req: ODataConfigReq | None = None):
    cfg = _load_odata_config()
    base_url = _normalize_base_url((req.base_url if req and req.base_url else cfg.get('base_url')))
    username = req.username if (req and req.username is not None) else (cfg.get('username') or None)
    password = req.password if (req and req.password is not None) else (cfg.get('password') or None)
    if not base_url:
        return {'status': 'error', 'message': 'Не указан base_url (введите и сохраните настройки).'}
    try:
        url = f"{base_url}/Catalog_Номенклатура"
        params = {
            '$format': 'json',
            '$filter': 'IsFolder eq true',
            '$select': 'Ref_Key,Code,Description,Parent_Key,IsFolder,Predefined,PredefinedDataName,DataVersion,DeletionMark',
            '$orderby': 'Code',
        }
        auth = (username, password) if (username or password) else None
        r = requests.get(url, params=params, auth=auth, timeout=60)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            text = r.content.decode('windows-1251', errors='replace')
            data = json.loads(text)

        OUTPUT_GROUPS.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT_GROUPS.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        vals = data.get('value', data)
        if isinstance(vals, dict):
            vals = [vals]
        total = len(vals) if isinstance(vals, list) else 0
        groups_count = sum(1 for v in vals if isinstance(v, dict) and v.get('IsFolder') is True)

        return {
            'status': 'ok',
            'message': 'Группы выгружены',
            'output': str(OUTPUT_GROUPS),
            'total': total,
            'groups_count': groups_count,
        }
    except Exception as e:
        return {'status': 'error', 'message': f'Ошибка выгрузки групп: {e}'}

@fastapi_app.post('/odata/reindex')
async def api_odata_reindex():
    # Заглушка: функция принудительной переиндексации находится в Streamlit UI и требует отдельной интеграции
    return {'status': 'not_implemented', 'message': 'Принудительная индексация номенклатуры пока не подключена в NiceGUI.'}

def _register_routes() -> None:
    """Импорт и регистрация страниц NiceGUI."""
    from .routes import register_routes as _register
    _register()


# Регистрируем UI-страницы при импорте модуля
_register_routes()


def run_dev(port: int = 8080, reload: bool = True, show: bool = False) -> None:
    """Запуск дев-сервера NiceGUI (горячая перезагрузка)."""
    ui.run(port=port, reload=reload, show=show)


# Экспортируем ASGI-приложение для продакшн запуска через uvicorn:
# uvicorn src.ui_nicegui.app:asgi_app --host 0.0.0.0 --port 8080
asgi_app = ng_app


if __name__ in {'__main__', '__mp_main__'}:
    run_dev()