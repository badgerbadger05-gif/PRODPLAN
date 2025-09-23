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
from typing import Optional, Tuple
import io
import pandas as pd
from pathlib import Path
import json
import requests
from src.odata_client import OData1CClient
from src.database import init_database, get_connection
from src.planner import generate_production_plan
from .services.plan_service import query_plan_overview_paginated, fetch_plan_dataset, query_plan_matrix_paginated, upsert_plan_entry, delete_plan_rows_for_item, bulk_upsert_plan_entries, ensure_root_product_by_code


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
    auto_width: Optional[bool] = True

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

class PlanMatrixReq(BaseModel):
    start_date: Optional[str] = None
    days: int = 30
    stage_id: Optional[int] = None
    page: int = 1
    page_size: int = 30
    sort_by: str = 'item_name'
    sort_dir: str = 'asc'
    db: Optional[str] = None

class UpsertPlanReq(BaseModel):
    item_id: int
    date: str
    qty: int
    stage_id: Optional[int] = None
    db: Optional[str] = None

class BulkUpsertEntry(BaseModel):
    item_id: int
    date: str
    qty: int
    stage_id: Optional[int] = None

class BulkUpsertReq(BaseModel):
    entries: list[BulkUpsertEntry] = []
    db: Optional[str] = None

class DeleteRowReq(BaseModel):
    item_id: int
    start_date: Optional[str] = None
    days: int = 30
    stage_id: Optional[int] = None
    db: Optional[str] = None


class EnsureItemReq(BaseModel):
    item_code: str
    item_name: Optional[str] = None
    item_article: Optional[str] = None
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
            out_path = Path(req.out) if req.out else Path('output/production_plan.xlsx')
            db_path = Path(req.db) if req.db else None
            result_path = generate_production_plan(
                db_path=db_path,
                output_path=out_path,
                horizon_days=int(req.days or 30),
                start_date=None if not req.start_date else __import__('datetime').date.fromisoformat(req.start_date),
                auto_width=True if req.auto_width is None else bool(req.auto_width),
            )
            print(f'[generate/plan] done: {result_path}')
        except Exception as e:
            print(f'[generate/plan] error: {e!r}')
    bg.add_task(_task)
    return {'status': 'accepted', 'message': 'Генерация плана запущена в фоне', 'out': req.out, 'auto_width': True if req.auto_width is None else bool(req.auto_width)}

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


@fastapi_app.post('/plan/matrix')
async def api_plan_matrix(req: PlanMatrixReq):
    data = query_plan_matrix_paginated(
        start_date_str=req.start_date or __import__('datetime').date.today().isoformat(),
        days=int(req.days or 30),
        stage_id=req.stage_id,
        page=int(req.page or 1),
        page_size=int(req.page_size or 30),
        sort_by=req.sort_by or 'item_name',
        sort_dir=req.sort_dir or 'asc',
        db_path=req.db,
    )
    return data

@fastapi_app.post('/plan/upsert')
async def api_plan_upsert(req: UpsertPlanReq):
    try:
        d = str(req.date or '').strip()
        qty = int(req.qty or 0)
        upsert_plan_entry(
            item_id=int(req.item_id),
            date_str=d,
            planned_qty=qty,
            stage_id=req.stage_id,
            db_path=req.db,
        )
        return {'status': 'ok'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@fastapi_app.post('/plan/ensure_item')
async def api_plan_ensure_item(req: EnsureItemReq):
    """
    Гарантирует наличие изделия в items и root_products.
    Возвращает item_id для дальнейшего редактирования матрицы.
    """
    try:
        item_id = ensure_root_product_by_code(
            item_code=str(req.item_code or '').strip(),
            item_name=(req.item_name or None),
            item_article=(req.item_article or None),
            db_path=req.db,
        )
        return {'status': 'ok', 'item_id': int(item_id)}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

@fastapi_app.post('/plan/bulk_upsert')
async def api_plan_bulk_upsert(req: BulkUpsertReq):
    try:
        payload = [
            {
                'item_id': int(e.item_id),
                'date': str(e.date),
                'qty': int(e.qty),
                'stage_id': (int(e.stage_id) if e.stage_id is not None and str(e.stage_id).strip() != '' else None),
            }
            for e in (req.entries or [])
        ]
        saved = bulk_upsert_plan_entries(payload, db_path=req.db)
        return {'status': 'ok', 'saved': int(saved)}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

@fastapi_app.post('/plan/delete_row')
async def api_plan_delete_row(req: DeleteRowReq):
    try:
        start_date = req.start_date or __import__('datetime').date.today().isoformat()
        deleted = delete_plan_rows_for_item(
            start_date_str=start_date,
            days=int(req.days or 30),
            item_id=int(req.item_id),
            stage_id=req.stage_id,
            db_path=req.db,
        )
        return {'status': 'ok', 'deleted': int(deleted)}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


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
# Nomenclature search API (for Rich Select / async search)
# ---------------------------
@fastapi_app.get('/nomenclature/search')
async def api_nomenclature_search(q: str = '', limit: int = 20):
    """
    Быстрый строковый поиск номенклатуры для UI (Rich Select / автокомплит).
    Источник: services.search_service.search_items_with_index (БД + локальный индекс).
    Возвращает {items: [{item_id?, item_name, item_article, item_code, label}], error?}
    """
    try:
        # Импорт внутри обработчика, чтобы избежать циклов импорта при старте
        from .services.search_service import search_items_with_index  # type: ignore
        limit = int(limit or 20)
        limit = max(1, min(50, limit))  # safety cap

        items = search_items_with_index(q, limit=limit)
        out = []
        for it in (items or []):
            name = str(it.get('item_name') or '')
            article = str(it.get('item_article') or '')
            code = str(it.get('item_code') or '')

            label_article = (article.strip() if article and article.strip() else '—')
            label = f"{name or '—'} — Арт. {label_article} ({code})"

            out.append({
                'item_id': it.get('item_id', None),
                'item_name': name,
                'item_article': article,
                'item_code': code,
                'label': label,
            })
        return {'items': out}
    except Exception as e:
        return {'items': [], 'error': str(e)}

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
# Полная выгрузка номенклатуры (результат синхронизации)
OUTPUT_NOMENCLATURE_FULL = Path('output') / 'odata_catalog_nomenclature_full.json'
# Файл отметки времени последней синхронизации
LAST_SYNC_PATH = Path('config') / 'last_sync_time.json'
# (опционально) выбранные группы каталога Номенклатура
GROUPS_SELECTED_PATH = Path('config') / 'odata_groups_selected.json'

def _build_auth(username: Optional[str], password: Optional[str]) -> Optional[Tuple[str, str]]:
    """
    Подготовка auth для requests. Возвращает (username, password) или None.
    """
    if username is None and password is None:
        return None
    return (str(username or ''), str(password or ''))


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

# Запись полной выгрузки номенклатуры в БД (таблица items)
def _upsert_nomenclature_to_db(items: list[dict], db_path: Optional[Path] = None) -> tuple[int, int]:
    """
    Записывает/обновляет записи каталога 1С Номенклатура в таблицу items:
      - item_code        ← Code
      - item_name        ← Description
      - item_article     ← Артикул
      - item_ref1c       ← Ref_Key
      - replenishment_method ← СпособПополнения
      - replenishment_time   ← СрокПополнения (int)
    Идемпотентно добавляет недостающие колонки в items.
    """
    inserted = 0
    updated = 0
    with get_connection(db_path) as conn:
        # Идемпотентно добавим недостающие колонки
        try:
            cols = conn.execute("PRAGMA table_info(items)").fetchall()
            col_names = {str(c[1]) for c in cols}
            if "item_article" not in col_names:
                conn.execute("ALTER TABLE items ADD COLUMN item_article TEXT")
            if "item_ref1c" not in col_names:
                conn.execute("ALTER TABLE items ADD COLUMN item_ref1c TEXT")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_items_ref1c ON items(item_ref1c)")
            if "replenishment_method" not in col_names:
                conn.execute("ALTER TABLE items ADD COLUMN replenishment_method TEXT")
            if "replenishment_time" not in col_names:
                conn.execute("ALTER TABLE items ADD COLUMN replenishment_time INTEGER")
        except Exception:
            # не роняем процесс синхронизации из‑за ALTER
            pass

        conn.execute("BEGIN")
        sql = """
        INSERT INTO items (item_code, item_name, item_article, item_ref1c, replenishment_method, replenishment_time, updated_at)
        VALUES (:code, :name, :article, :ref1c, :replenishment_method, :replenishment_time, datetime('now'))
        ON CONFLICT(item_code) DO UPDATE SET
            item_name = excluded.item_name,
            item_article = excluded.item_article,
            item_ref1c = COALESCE(excluded.item_ref1c, item_ref1c),
            replenishment_method = excluded.replenishment_method,
            replenishment_time = excluded.replenishment_time,
            updated_at = datetime('now')
        """
        for it in items:
            code = str(it.get('Code') or '').strip()
            name = str(it.get('Description') or '').strip()
            article = it.get('Артикул')
            article = None if article is None else str(article).strip()
            ref1c = str(it.get('Ref_Key') or '').strip() or None
            repl_method = it.get('СпособПополнения')
            repl_method = None if repl_method is None else str(repl_method).strip()
            repl_time_raw = it.get('СрокПополнения')
            try:
                repl_time = int(repl_time_raw) if repl_time_raw is not None and str(repl_time_raw).strip() != '' else None
            except Exception:
                repl_time = None

            if not code or not name:
                continue

            existed = conn.execute("SELECT 1 FROM items WHERE item_code = ?", (code,)).fetchone() is not None
            conn.execute(sql, {
                "code": code,
                "name": name,
                "article": article,
                "ref1c": ref1c,
                "replenishment_method": repl_method,
                "replenishment_time": repl_time,
            })
            if existed:
                updated += 1
            else:
                inserted += 1
        conn.commit()
    return inserted, updated

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
        auth = _build_auth(username, password)
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

# Хранилище статуса синхронизации
_sync_status = {
    'running': False,
    'progress': 0,
    'message': 'Готов к синхронизации'
}

@fastapi_app.post('/nomenclature/sync')
async def api_nomenclature_sync(bg: BackgroundTasks):
    def _task():
        global _sync_status
        try:
            _sync_status['running'] = True
            _sync_status['progress'] = 0
            _sync_status['message'] = 'Подготовка…'
            print('[nomenclature/sync] started')

            # Загружаем конфиг OData
            cfg = _load_odata_config()
            base_url = _normalize_base_url(cfg.get('base_url'))
            username = cfg.get('username') or None
            password = cfg.get('password') or None

            if not base_url:
                _sync_status['message'] = 'Ошибка: не указан base_url в config/odata_config.json'
                return

            url = f"{base_url}/Catalog_Номенклатура"
            auth = _build_auth(username, password)

            # Попытка получить общее количество записей
            total = None
            try:
                params_count = {'$format': 'json', '$count': 'true', '$top': 1, '$filter': 'IsFolder eq false'}
                rc = requests.get(url, params=params_count, auth=auth, timeout=60)
                rc.raise_for_status()
                try:
                    data_c = rc.json()
                except Exception:
                    text_c = rc.content.decode('windows-1251', errors='replace')
                    data_c = json.loads(text_c)
                for k in ('@odata.count', 'odata.count', 'Count', 'count', '@count'):
                    if isinstance(data_c, dict) and k in data_c and str(data_c[k]).isdigit():
                        total = int(data_c[k])
                        break
            except Exception:
                total = None  # сервер мог не поддержать $count — работаем без него

            # Пагинация
            top = 1000
            skip = 0
            page_no = 0
            items = []

            while True:
                _sync_status['message'] = f'Загрузка страницы {page_no + 1}…'
                params = {
                    '$format': 'json',
                    '$select': 'Ref_Key,Code,Description,Артикул,СпособПополнения,СрокПополнения,ЕдиницаИзмерения_Key,КатегорияНоменклатуры_Key,ТипНоменклатуры',
                    '$orderby': 'Code',
                    '$top': top,
                    '$skip': skip
                }
                try:
                    r = requests.get(url, params=params, auth=auth, timeout=120)
                    r.raise_for_status()
                    try:
                        data = r.json()
                    except Exception:
                        text = r.content.decode('windows-1251', errors='replace')
                        data = json.loads(text)
                except Exception as e:
                    _sync_status['message'] = f'Ошибка загрузки: {e}'
                    print(f'[nomenclature/sync] page error: {e!r}')
                    break

                vals = data.get('value', data)
                if isinstance(vals, dict):
                    vals = [vals]
                if not vals or not isinstance(vals, list):
                    break

                # Накопление результатов
                for v in vals:
                    if not isinstance(v, dict):
                        continue
                    # Отсекаем группы (папки) на клиенте для надежности
                    if v.get('IsFolder') is True:
                        continue
                    items.append({
                        'Ref_Key': v.get('Ref_Key'),
                        'Code': v.get('Code'),
                        'Description': v.get('Description'),
                        'Артикул': v.get('Артикул'),
                        'Parent_Key': v.get('Parent_Key'),
                    })

                skip += len(vals)
                page_no += 1

                # Обновление прогресса
                if total:
                    prog = min(95, max(1, int(skip * 100 / max(1, total))))
                else:
                    # При неизвестном total — плавный рост с потолком до 95%
                    prog = min(95, 5 + page_no * 3)
                _sync_status['progress'] = prog
                _sync_status['message'] = f'Получено: {len(items)}'

                # Признак последней страницы
                if len(vals) < top:
                    break
                # Защита от бесконечного цикла
                if page_no >= 2000:
                    _sync_status['message'] = 'Достигнут предел страниц (safety cap)'
                    break

            # Сохранение результата
            try:
                OUTPUT_NOMENCLATURE_FULL.parent.mkdir(parents=True, exist_ok=True)
                with OUTPUT_NOMENCLATURE_FULL.open('w', encoding='utf-8') as f:
                    json.dump({'value': items, 'total': len(items)}, f, ensure_ascii=False, indent=2)
            except Exception as e:
                _sync_status['message'] = f'Ошибка сохранения: {e}'
                print(f'[nomenclature/sync] save error: {e!r}')

            # Запись полной выгрузки в БД выполняется ниже через _upsert_nomenclature_to_db(items)

            # Запись в БД items (полная выгрузка сущности)
            try:
                _sync_status['message'] = 'Запись в БД…'
                _sync_status['progress'] = min(99, max(_sync_status.get('progress', 0), 96))
                inserted, updated = _upsert_nomenclature_to_db(items)
                _sync_status['message'] = f'В БД: вставлено {inserted}, обновлено {updated}'
            except Exception as e:
                _sync_status['message'] = f'Ошибка записи в БД: {e}'
                print(f'[nomenclature/sync] db upsert error: {e!r}')

            # Отметка времени последней синхронизации
            try:
                now_iso = __import__('datetime').datetime.now().isoformat()
                LAST_SYNC_PATH.parent.mkdir(parents=True, exist_ok=True)
                with LAST_SYNC_PATH.open('w', encoding='utf-8') as f:
                    json.dump({'last_sync': now_iso}, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f'[nomenclature/sync] last_sync update error: {e!r}')

            _sync_status['progress'] = 100
            _sync_status['message'] = f'Синхронизация завершена: {len(items)} позиций'
            print(f'[nomenclature/sync] done: {len(items)} items -> {OUTPUT_NOMENCLATURE_FULL}')
        except Exception as e:
            _sync_status['message'] = f'Ошибка: {e}'
            print(f'[nomenclature/sync] error: {e!r}')
        finally:
            _sync_status['running'] = False
            # Сбрасываем прогресс через 3 секунды
            import threading
            import time as _time
            def reset_progress():
                _time.sleep(3)
                if not _sync_status['running']:
                    _sync_status['progress'] = 0
                    _sync_status['message'] = 'Готов к синхронизации'
            threading.Thread(target=reset_progress, daemon=True).start()
    bg.add_task(_task)
    return {'status': 'accepted', 'message': 'Синхронизация номенклатуры запущена в фоне'}

@fastapi_app.get('/nomenclature/sync/status')
async def api_nomenclature_sync_status():
    global _sync_status
    return _sync_status

def _register_routes() -> None:
    """Импорт и регистрация страниц NiceGUI."""
    from .routes import register_routes as _register
    _register()


# Регистрируем UI-страницы при импорте модуля
_register_routes()


def run_dev(port: int = 8080, reload: bool = True, show: bool = False) -> None:
    """Запуск дев-сервера NiceGUI (горячая перезагрузка)."""
    ui.run(port=port, reload=reload, show=show)


# Функция для автоматической синхронизации по расписанию
def schedule_nomenclature_sync():
    try:
        from pathlib import Path
        import json
        import datetime
        from datetime import time as dt_time
        
        # Загружаем настройки синхронизации
        sync_cfg_path = Path('config') / 'nomenclature_sync_config.json'
        if sync_cfg_path.exists():
            sync_cfg = json.loads(sync_cfg_path.read_text('utf-8'))
            interval_hours = int(sync_cfg.get('interval_hours', 1))
            start_time_str = str(sync_cfg.get('start_time', '09:00'))
            
            # Парсим время начала
            try:
                start_time = datetime.datetime.strptime(start_time_str, '%H:%M').time()
            except ValueError:
                start_time = dt_time(9, 0)  # По умолчанию 09:00
            
            # Получаем текущее время
            now = datetime.datetime.now()
            current_time = now.time()
            current_date = now.date()
            
            # Загружаем время последней синхронизации
            last_sync_path = Path('config') / 'last_sync_time.json'
            last_sync_time = None
            if last_sync_path.exists():
                try:
                    last_sync_data = json.loads(last_sync_path.read_text('utf-8'))
                    last_sync_str = last_sync_data.get('last_sync')
                    if last_sync_str:
                        last_sync_time = datetime.datetime.fromisoformat(last_sync_str)
                except Exception:
                    pass
            
            # Проверяем, нужно ли запустить синхронизацию
            should_sync = False
            
            # Если время последней синхронизации не сегодня, проверяем время начала
            if last_sync_time is None or last_sync_time.date() != current_date:
                if current_time >= start_time:
                    should_sync = True
            # Если сегодня еще не было синхронизации и прошло достаточно времени с момента последней синхронизации
            elif last_sync_time.date() == current_date:
                # Проверяем, прошел ли интервал с момента последней синхронизации
                time_since_last_sync = now - last_sync_time
                if time_since_last_sync.total_seconds() >= interval_hours * 3600:
                    should_sync = True
            
            if should_sync:
                print(f'[schedule] Starting nomenclature sync at {now}')
                # Здесь будет вызов функции синхронизации
                # Пока что просто выводим информацию в консоль
                
                # Обновляем время последней синхронизации
                try:
                    last_sync_path.parent.mkdir(parents=True, exist_ok=True)
                    last_sync_path.write_text(
                        json.dumps({'last_sync': now.isoformat()}, ensure_ascii=False, indent=2),
                        encoding='utf-8'
                    )
                except Exception as e:
                    print(f'[schedule] Error updating last sync time: {e!r}')
        else:
            print('[schedule] Nomenclature sync config not found')
    except Exception as e:
        print(f'[schedule] Error in nomenclature sync scheduler: {e!r}')


# Экспортируем ASGI-приложение для продакшн запуска через uvicorn:
# uvicorn src.ui_nicegui.app:asgi_app --host 0.0.0.0 --port 8080
asgi_app = ng_app


if __name__ in {'__main__', '__mp_main__'}:
    # Запускаем планировщик синхронизации
    import threading
    import time
    
    def scheduler_loop():
        while True:
            schedule_nomenclature_sync()
            time.sleep(60)  # Проверяем расписание каждую минуту
    
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()
    
    run_dev()