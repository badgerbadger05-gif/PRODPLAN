# PRODPLAN Streamlit UI: Просмотр "План производства" напрямую из БД
# Запуск: streamlit run src/ui.py

import datetime as dt
import json
import sqlite3
from pathlib import Path
import pandas as pd
import streamlit as st
from dataclasses import asdict
import time
import subprocess
import shutil
from urllib import request as _urlreq, error as _urlerr
import urllib.parse as _urlparse
import numpy as np
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.planner import generate_plan_dataframe
from src.database import get_connection, init_database
from src.bom_calculator import get_root_products, explode_bom_for_root
from src.odata_client import OData1CClient


# ============================
# LLM (Ollama) health-check & autostart
# ============================
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_HEALTH_PATH = "/api/tags"

def _ollama_is_healthy(timeout: float = 1.5) -> bool:
    try:
        req = _urlreq.Request(f"{OLLAMA_BASE_URL}{OLLAMA_HEALTH_PATH}")
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(code) < 300
    except Exception:
        return False

def _start_ollama_background() -> bool:
    """
    Пытается запустить 'ollama serve' в фоне (Windows, detached).
    Возвращает True, если команда стартовала без исключений.
    """
    exe = shutil.which("ollama")
    if not exe:
        return False
    try:
        # Попытка детачить процесс на Windows; параметры игнорируются на POSIX
        creationflags = 0
        try:
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        except Exception:
            creationflags = 0
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
            shell=False,
        )
        return True
    except Exception:
        return False

def ensure_ollama_running(max_wait_seconds: float = 30.0, poll_interval: float = 1.0):
    """
    Проверяет доступность Ollama и при необходимости пытается запустить.
    Возвращает кортеж (ok: bool, message: str)
    """
    if _ollama_is_healthy():
        return True, "Ollama уже отвечает"
    started = _start_ollama_background()
    if not started:
        return False, "Не удалось запустить 'ollama serve' (исполняемый файл не найден или отказ запуска)"
    deadline = time.time() + float(max_wait_seconds)
    while time.time() < deadline:
        if _ollama_is_healthy():
            return True, "Ollama запущена и отвечает"
        time.sleep(float(poll_interval))
    return False, f"Ollama не ответила за {int(max_wait_seconds)}с на {OLLAMA_HEALTH_PATH}"

# ============================
# Локальный индекс номенклатуры (OData 1С → эмбеддинги через Ollama)
# ============================
OLLAMA_EMBED_MODEL = "nomic-embed-text"
NOMEN_INDEX_PATH = Path("output/nomenclature_index.json")

def _ensure_output_dir():
    try:
        Path("output").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def _read_nomen_index() -> dict:
    try:
        if NOMEN_INDEX_PATH.exists():
            with NOMEN_INDEX_PATH.open("r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        return {}
    return {}

def _write_nomen_index(data: dict) -> None:
    try:
        _ensure_output_dir()
        with NOMEN_INDEX_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _ollama_embed_text(text: str, timeout: float = 60.0) -> list[float] | None:
    try:
        url = f"{OLLAMA_BASE_URL}/api/embeddings"
        payload = json.dumps({"model": OLLAMA_EMBED_MODEL, "prompt": str(text)}).encode("utf-8")
        req = _urlreq.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw)
            if isinstance(data, dict):
                if "embedding" in data and isinstance(data["embedding"], list):
                    return [float(x) for x in data["embedding"]]
                if "data" in data and isinstance(data["data"], list) and data["data"]:
                    emb = data["data"][0].get("embedding")
                    if isinstance(emb, list):
                        return [float(x) for x in emb]
    except Exception:
        return None
    return None

def _extract_count(resp: dict) -> int | None:
    """
    Попытка извлечь количество записей из ответа OData.
    Поддерживаются ключи вида '@odata.count', 'odata.count', 'Count', 'count'.
    """
    if not isinstance(resp, dict):
        return None
    for k in ("@odata.count", "odata.count", "Count", "count"):
        if k in resp:
            try:
                return int(resp.get(k))
            except Exception:
                continue
    # Некоторые серверы кладут count в metadata-поле — пропускаем
    return None

def _try_get_nomenclature_count() -> int | None:
    """
    Получить общее количество записей Catalog_Номенклатура через OData ($count=true/$inlinecount).
    Возвращает None, если сервер не поддерживает подсчёт.
    """
    cfg = _load_odata_config()
    base_url = (cfg.get("base_url") or "").strip()
    username = (cfg.get("username") or "").strip() or None
    password = (cfg.get("password") or "").strip() or None
    if not base_url:
        return None
    client = OData1CClient(base_url=base_url, username=username, password=password, token=None)
    # Попытка 1: $count=true, $top=0
    try:
        resp = client._make_request("Catalog_Номенклатура", {"$count": "true", "$top": 0})
        cnt = _extract_count(resp)
        if isinstance(cnt, int) and cnt >= 0:
            return cnt
    except Exception:
        pass
    # Попытка 2: $inlinecount=allpages, $top=1 (совместимость со старыми реализациями)
    try:
        resp2 = client._make_request("Catalog_Номенклатура", {"$inlinecount": "allpages", "$top": 1})
        cnt = _extract_count(resp2)
        if isinstance(cnt, int) and cnt >= 0:
            return cnt
    except Exception:
        pass
    return None

def _fetch_nomenclature_from_1c(limit: int = 30000, on_progress=None) -> tuple[list[dict], int | None]:
    """
    Загружает номенклатуру из 1С OData: поля Code, Description, Артикул.
    Читает базовые параметры из config/odata_config.json.
    Возвращает (список_записей, total_count_или_None). Коллбек on_progress сообщает ход загрузки.
    """
    cfg = _load_odata_config()
    base_url = (cfg.get("base_url") or "").strip()
    username = (cfg.get("username") or "").strip() or None
    password = (cfg.get("password") or "").strip() or None
    if not base_url:
        return [], None
    client = OData1CClient(base_url=base_url, username=username, password=password, token=None)

    total_count = _try_get_nomenclature_count()
    if isinstance(total_count, int) and total_count >= 0:
        total_count = min(total_count, int(limit))
    else:
        total_count = None

    out: list[dict] = []
    top = 1000
    skip = 0
    max_pages = max(1, int(limit // top) + 2)
    pages = 0
    while len(out) < limit and pages < max_pages:
        params = {
            "$select": "Code,Description,Артикул",
            "$top": top,
            "$skip": skip,
        }
        try:
            resp = client._make_request("Catalog_Номенклатура", params)
        except Exception:
            break
        rows = []
        if isinstance(resp, dict) and isinstance(resp.get("value"), list):
            rows = resp.get("value", [])
        elif resp:
            rows = [resp]
        if not rows:
            break
        last_name = ""
        last_code = ""
        for r in rows:
            try:
                code = str(r.get("Code") or "").strip()
                name = str(r.get("Description") or "").strip()
                article = str(r.get("Артикул") or "").strip()
                if code or name or article:
                    out.append({"code": code, "name": name, "article": article})
                    last_name = name
                    last_code = code
                    if len(out) >= limit:
                        break
            except Exception:
                continue
        # Прогресс по загрузке из OData (постранично)
        if callable(on_progress):
            try:
                on_progress(len(out), total_count, {"phase": "fetch", "last_name": last_name, "last_code": last_code})
            except Exception:
                pass
        if len(rows) < top:
            break
        skip += len(rows)
        pages += 1
    return out, total_count
def _compute_item_hash(name: str, article: str, code: str) -> str:
    """
    Хеш содержимого карточки для инкрементальной индексации.
    Нормализуем регистр и пробелы.
    """
    def norm(x: str) -> str:
        return " ".join((x or "").strip().split()).upper()
    payload = f"{norm(name)}|{norm(article)}|{norm(code)}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _embed_items_parallel(items_to_embed: list[dict], max_workers: int, on_progress, total_embed: int) -> tuple[list[dict], int]:
    """
    Параллельный расчёт эмбеддингов для списка элементов.
    items_to_embed: [{code,name,article,hash}]
    Возвращает (список_успешных_элементов, failed_count).
    """
    results: list[dict] = []
    failed = 0
    processed = 0
    # Выполняем запросы в пуле потоков
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as ex:
        futures = {}
        for it in items_to_embed:
            txt = " | ".join([s for s in [it.get("name") or "", it.get("article") or "", it.get("code") or ""] if s])
            futures[ex.submit(_ollama_embed_text, txt)] = it
        for fut in as_completed(futures):
            it = futures[fut]
            vec = None
            try:
                vec = fut.result()
            except Exception:
                vec = None
            if vec is not None:
                results.append({
                    "code": it.get("code") or "",
                    "name": it.get("name") or "",
                    "article": it.get("article") or "",
                    "vector": vec,
                    "hash": it.get("hash") or "",
                })
            else:
                failed += 1
            processed += 1
            if callable(on_progress):
                try:
                    on_progress(processed, total_embed, {"phase": "embed", "name": it.get("name") or "", "article": it.get("article") or "", "code": it.get("code") or ""})
                except Exception:
                    pass
    return results, failed

def ensure_llama_index_daily(on_progress=None, max_workers: int = 4) -> tuple[bool, str, bool]:
    """
    Ежесуточное построение локального семантического индекса номенклатуры с инкрементальной переиндексацией.
    on_progress(processed:int, total:int|None, info:dict) — коллбек для прогресса.
    Возвращает (ok, message, skipped).
    """
    started_at = time.time()

    # Считываем прошлый индекс
    idx = _read_nomen_index()
    last_ts = None
    try:
        last_ts = idx.get("meta", {}).get("last_indexed_at")
    except Exception:
        last_ts = None

    now = dt.datetime.utcnow()
    if last_ts:
        try:
            prev = dt.datetime.fromisoformat(str(last_ts).replace("Z", ""))
            if (now - prev).total_seconds() < 24 * 3600:
                return True, f"Индекс не требуется (последний запуск: {prev.isoformat()}Z)", True
        except Exception:
            pass

    if not _ollama_is_healthy():
        return False, "Ollama недоступна для вычисления эмбеддингов", False

    # 1) Загружаем каталог из 1С (с прогрессом по страницам)
    items, total_count = _fetch_nomenclature_from_1c(limit=30000, on_progress=on_progress)
    if not items:
        return False, "Не удалось загрузить номенклатуру из 1С", False

    # 2) Готовим map по старому индексу
    old_items = []
    try:
        old_items = idx.get("items") or []
    except Exception:
        old_items = []
    old_by_code: dict[str, dict] = {}
    for e in old_items:
        try:
            code = str(e.get("code") or "")
            if code:
                old_by_code[code] = e
        except Exception:
            continue

    # 3) Сравниваем и формируем списки для переиспользования/переиндексации
    new_by_code: dict[str, dict] = {}
    to_embed: list[dict] = []
    reused = 0
    for it in items:
        code = str(it.get("code") or "")
        name = str(it.get("name") or "")
        article = str(it.get("article") or "")
        if not code and not name and not article:
            continue
        h = _compute_item_hash(name, article, code)
        rec = {"code": code, "name": name, "article": article, "hash": h}
        old = old_by_code.get(code)
        if old and str(old.get("hash") or "") == h and isinstance(old.get("vector"), list):
            # Не изменилось — переиспользуем вектор
            rec["vector"] = old.get("vector")
            reused += 1
        else:
            # Требуется пересчёт эмбеддинга
            to_embed.append(rec)
        new_by_code[code] = rec

    removed_codes = [c for c in old_by_code.keys() if c not in new_by_code]

    # 4) Пересчитываем эмбеддинги параллельно только для изменённых/новых
    embedded: list[dict] = []
    failed = 0
    total_embed = len(to_embed)
    if total_embed > 0:
        embedded, failed = _embed_items_parallel(to_embed, max_workers=max_workers, on_progress=on_progress, total_embed=total_embed)

    # 5) Собираем финальный список
    emb_by_code = {e["code"]: e for e in embedded}
    final_items: list[dict] = []
    for code, rec in new_by_code.items():
        if "vector" in rec and isinstance(rec["vector"], list):
            final_items.append({"code": rec["code"], "name": rec["name"], "article": rec["article"], "vector": rec["vector"], "hash": rec["hash"]})
        else:
            emb = emb_by_code.get(code)
            if emb and isinstance(emb.get("vector"), list):
                final_items.append({"code": rec["code"], "name": rec["name"], "article": rec["article"], "vector": emb["vector"], "hash": rec["hash"]})
            # Иначе пропускаем безвекторные записи (не попадут в индекс)

    if not final_items:
        return False, "Эмбеддинги не получены (возможно, модель не загружена)", False

    # 6) Сохраняем индекс и статистику
    duration = int(time.time() - started_at)
    data = {
        "meta": {
            "model": OLLAMA_EMBED_MODEL,
            "last_indexed_at": now.isoformat() + "Z",
            "count": len(final_items),
            "stats": {
                "total_catalog": len(items),
                "unchanged_reused": reused,
                "reembedded": len(embedded),
                "failed": int(failed),
                "removed": len(removed_codes),
                "duration_sec": duration,
                "total_count_hint": total_count if isinstance(total_count, int) else None,
                "max_workers": int(max_workers),
            },
        },
        "items": final_items,
    }
    _write_nomen_index(data)

    msg = f"Индекс готов: всего={len(items)}, переиспользовано={reused}, переиндексировано={len(embedded)}, удалено={len(removed_codes)}, ошибки={failed}, {duration}с"
    return True, msg, False

def _llama_search_nomenclature(query: str, limit: int = 10) -> list[dict]:
    """
    Локальный семантический поиск по индексу: эмбеддинг запроса → косинусная близость.
    Возвращает [{name, code}] для UI. Фолбэк на БД выполняется выше по стеку.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    idx = _read_nomen_index()
    items = idx.get("items") if isinstance(idx, dict) else None
    if not items:
        return []

    qvec = _ollama_embed_text(q)
    if not qvec:
        return []

    try:
        M = np.array([it.get("vector", []) for it in items], dtype=np.float32)
        qv = np.array(qvec, dtype=np.float32)
        if M.ndim != 2 or qv.ndim != 1 or M.shape[1] != qv.shape[0]:
            return []
        denom = (np.linalg.norm(M, axis=1) + 1e-9) * (np.linalg.norm(qv) + 1e-9)
        sims = (M @ qv) / denom
        top_idx = np.argsort(-sims)[:max(1, limit)]
        out: list[dict] = []
        for i in top_idx:
            try:
                it = items[int(i)]
                name = str(it.get("name") or "")
                code = str(it.get("code") or "")
                out.append({"name": name, "code": code})
            except Exception:
                continue
        return out
    except Exception:
        return []

def _db_search_nomenclature(query: str, limit: int = 10) -> list[dict]:
    like = f"%{query}%"
    with get_connection(None) as conn:
        try:
            rows = conn.execute(
                "SELECT item_name, item_code FROM items WHERE item_name LIKE ? OR item_code LIKE ? LIMIT 20",
                (like, like),
            ).fetchall()
        except Exception:
            return []
    out: list[dict] = []
    for r in rows:
        name = str(r[0] or "")
        code_v = str(r[1] or "")
        if not code_v:
            continue
        out.append({"name": name, "code": code_v})
        if len(out) >= limit:
            break
    return out

def _ensure_item_and_add_to_roots(code: str, name: str = "") -> None:
    code = (code or "").strip()
    name = (name or "").strip() or code
    if not code:
        raise ValueError("Пустой артикул")
    with get_connection(None) as conn:
        row = conn.execute("SELECT item_id FROM items WHERE item_code = ?", (code,)).fetchone()
        if row:
            item_id = int(row[0])
        else:
            cur = conn.execute(
                "INSERT INTO items (item_code, item_name) VALUES (?, ?)",
                (code, name),
            )
            item_id = int(cur.lastrowid)
        conn.execute("INSERT OR IGNORE INTO root_products (item_id) VALUES (?)", (item_id,))
        conn.commit()

def _delete_items_from_plan_by_codes(codes: list[str]) -> None:
    if not codes:
        return
    codes = [str(c).strip() for c in codes if str(c).strip()]
    if not codes:
        return
    placeholders = ",".join("?" for _ in codes)
    with get_connection(None) as conn:
        rows = conn.execute(f"SELECT item_id FROM items WHERE item_code IN ({placeholders})", codes).fetchall()
        item_ids = [int(r[0]) for r in rows]
        if not item_ids:
            return
        ph2 = ",".join("?" for _ in item_ids)
        conn.execute(f"DELETE FROM root_products WHERE item_id IN ({ph2})", item_ids)
        conn.execute(f"DELETE FROM production_plan_entries WHERE item_id IN ({ph2})", item_ids)
        conn.commit()

def _is_date_header(col: str) -> bool:
    # Заголовки дат формата dd.mm.yy (ровно 8 символов: 10.09.25)
    if not isinstance(col, str) or len(col) != 8:
        return False
    if col[2] != "." or col[5] != ".":
        return False
    try:
        dt.datetime.strptime(col, "%d.%m.%y")
        return True
    except Exception:
        return False


def _style_weekends(df: pd.DataFrame):
    # Подсветка выходных колонок серым фоном
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    for col in df.columns:
        if _is_date_header(col):
            d = dt.datetime.strptime(col, "%d.%m.%y").date()
            if d.weekday() >= 5:  # 5=Сб, 6=Вс
                styles[col] = "background-color: #E6E6E6"
    # Возвращаем таблицу стилей той же формы
    return styles


def _get_last_stock_sync_at() -> str | None:
    """
    Возвращает дату/время последнего обновления остатков.
    Приоритет:
      1) MAX(recorded_at) из stock_history (если включали sync с историей)
      2) MAX(updated_at) из items, где stock_qty IS NOT NULL
    """
    with get_connection(None) as conn:
        # 1) История остатков
        try:
            row = conn.execute("SELECT MAX(recorded_at) FROM stock_history").fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
        # 2) По карточкам
        try:
            row2 = conn.execute("SELECT MAX(updated_at) FROM items WHERE stock_qty IS NOT NULL").fetchone()
            if row2 and row2[0]:
                return str(row2[0])
        except Exception:
            pass
    return None

# Конфигурация OData: чтение/запись
CONFIG_DIR = Path("config")
CONFIG_PATH = CONFIG_DIR / "odata_config.json"

def _load_odata_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            import json as _json
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                data = _json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}

def _save_odata_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        # В UI покажем ошибку при сохранении
        pass

def _mask_secret(value: str | None) -> str:
    return "••••••" if value else ""

def _format_prefixes(prefixes: dict | None) -> str:
    """
    Превращаем словарь префиксов вида {"item_code": ["AB","CD"]} в многострочный текст:
      item_code=AB,CD
    """
    if not isinstance(prefixes, dict):
        return ""
    lines: list[str] = []
    for k, arr in prefixes.items():
        if isinstance(arr, (list, tuple)):
            vals = [str(x).strip() for x in arr if str(x).strip()]
            lines.append(f"{k}=" + ",".join(vals))
    return "\n".join(lines)

def _parse_prefixes(text: str) -> dict[str, list[str]]:
    """
    Разбираем многострочный текст формата:
      key=pre1,pre2
      another=AA,BB
    -> {"key":["pre1","pre2"], "another":["AA","BB"]}
    Пропускаем пустые строки и комментарии, начинающиеся с #.
    """
    out: dict[str, list[str]] = {}
    if not text:
        return out
    for raw in text.splitlines():
        line = (raw or "").strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        vals = [p.strip() for p in v.split(",") if p.strip()]
        if key:
            out[key] = vals
    return out


def main():
    st.set_page_config(page_title="PRODPLAN — План производства", layout="wide")
    # Гарантируем, что все таблицы (включая production_areas) созданы
    try:
        init_database()
    except Exception:
        pass
     
    # Проверка и автозапуск Ollama (локальный LLM)
    if "ollama_check_done" not in st.session_state:
        try:
            with st.spinner("Проверка и запуск Ollama…"):
                ok, msg = ensure_ollama_running(max_wait_seconds=30.0, poll_interval=1.0)
            if ok:
                st.success(f"LLM: {msg}")
            else:
                st.warning(f"LLM: {msg}")
            st.session_state["ollama_check_done"] = True
            st.session_state["ollama_check_status"] = "ok" if ok else "warn"
            st.session_state["ollama_check_message"] = msg
        except Exception as _e:
            st.warning(f"LLM: сбой проверки/запуска: {_e}")
            st.session_state["ollama_check_done"] = True
            st.session_state["ollama_check_status"] = "warn"
            st.session_state["ollama_check_message"] = str(_e)
    else:
        _status = st.session_state.get("ollama_check_status", "")
        _msg = st.session_state.get("ollama_check_message", "")
        if _status == "ok":
            st.caption(f"LLM готов: {_msg}")
        else:
            st.caption(f"LLM предупреждение: {_msg}")
    
    # Ежесуточная индексация номенклатуры (через Ollama) — с прогрессом в UI
    if "llama_index_checked" not in st.session_state:
        if _ollama_is_healthy():
            try:
                st.write("LLM: индексация номенклатуры…")
                progress_text = st.empty()
                current_line = st.empty()
                bar = st.progress(0)

                def _on_progress(processed: int, total: int | None, info: dict):
                    phase = str(info.get("phase", "") or "")
                    # Обновление прогресса: если общий объём известен — используем прогресс‑бар
                    if isinstance(total, int) and total > 0:
                        pct = int(max(0, min(100, processed * 100.0 / total)))
                        bar.progress(pct)
                        progress_text.markdown(f"Этап: {phase} • обработано {processed} из {total}")
                    else:
                        # Если общего объёма нет — только счётчик без прогресс‑бара
                        progress_text.markdown(f"Этап: {phase} • обработано {processed}")
                    # Текущая позиция
                    name = str(info.get("name") or info.get("last_name") or "")
                    code = str(info.get("code") or info.get("last_code") or "")
                    article = str(info.get("article") or "")
                    parts = [p for p in [name, article, code] if p]
                    current_line.markdown("Текущая позиция: " + (" | ".join(parts) if parts else "—"))

                ok_idx, idx_msg, skipped = ensure_llama_index_daily(on_progress=_on_progress)

                # Очистим прогрессовые элементы
                try:
                    bar.empty()
                    current_line.empty()
                    progress_text.empty()
                except Exception:
                    pass

                if ok_idx and skipped:
                    st.caption(f"LLM: {idx_msg}")
                elif ok_idx:
                    st.success(f"LLM: {idx_msg}")
                else:
                    st.warning(f"LLM: {idx_msg}")
            except Exception as e:
                st.warning(f"LLM: сбой индексации: {e}")
        st.session_state["llama_index_checked"] = True
     
    # Уменьшаем шрифты в сайдбаре
    st.markdown("""
        <style>
        section[data-testid="stSidebar"] {
            font-size: 0.9rem !important;
        }
        section[data-testid="stSidebar"] * {
            font-size: 0.9rem !important;
        }
        section[data-testid="stSidebar"] .stRadio label {
            font-size: 0.9rem !important;
            margin: 0.1rem 0 !important;
            padding: 0 !important;
        }
        /* Единый стиль заголовков блоков в сайдбаре */
        section[data-testid="stSidebar"] h1,
        section[data-testid="stSidebar"] h2,
        section[data-testid="stSidebar"] h3 {
            font-size: 1.1rem !important;   /* на шаг больше базового 0.9rem */
            margin: 0.25rem 0 0.5rem 0 !important;
        }
        section[data-testid="stSidebar"] .stDateInput label,
        section[data-testid="stSidebar"] .stNumberInput label,
        section[data-testid="stSidebar"] .stTextInput label,
        section[data-testid="stSidebar"] .stCheckbox label {
            font-size: 0.9rem !important;
        }
        section[data-testid="stSidebar"] .stButton button {
            font-size: 0.9rem !important;
        }
        section[data-testid="stSidebar"] .stSelectbox label {
            font-size: 0.9rem !important;
        }
        </style>
    """, unsafe_allow_html=True)

    with st.sidebar:
        st.subheader("Навигация")
        try:
            page = st.radio(
                "",
                ["План производства", "Этапы", "Ресурсы", "Параметры синхронизации БД"],
                index=0,
                label_visibility="collapsed",
                key="nav_radio",
            )
        except TypeError:
            # Fallback для старых версий Streamlit без параметра label_visibility
            page = st.radio(
                "",
                ["План производства", "Этапы", "Ресурсы", "Параметры синхронизации БД"],
                index=0,
                key="nav_radio",
            )

        st.divider()
        # Единственная операция в сайдбаре: быстрый запуск выгрузки остатков из 1С (OData)
        if st.button("Выгрузить остатки из 1С", type="primary", key="btn_sync_odata_quick"):
            from src.database import init_database
            from src.odata_stock_sync import sync_stock_from_odata

            # Читаем сохранённые параметры из config/odata_config.json
            cfg = _load_odata_config()
            base_url = (cfg.get("base_url") or "").strip()
            entity_name = (cfg.get("entity_name") or "").strip()
            username = (cfg.get("username") or "").strip() or None
            password = (cfg.get("password") or "").strip() or None

            # Нормализация base_url: убрать завершающий $metadata, если ошибочно указан
            if base_url.lower().endswith("$metadata"):
                base_url = base_url[: -len("$metadata")].rstrip("/")

            # Поля для выборки отключены на быстрый запуск (избегаем $expand и вложенных путей, ведущих к 400)
            select_fields = None

            # Валидация сущности
            if not base_url or not entity_name:
                st.warning("Сначала заполните и сохраните настройки на странице «Параметры синхронизации БД».")
            elif "$metadata" in entity_name.lower():
                st.error("В поле «Сущность/остатки (EntitySet)» укажите имя набора (например, AccumulationRegister_ЗапасыНаСкладах), без «$metadata».")
            else:
                init_database()
                try:
                    with st.spinner("Выгрузка остатков из 1С…"):
                        stats = sync_stock_from_odata(
                            base_url=base_url,
                            entity_name=entity_name,
                            username=username,
                            password=password,
                            token=None,
                            filter_query=None,
                            select_fields=select_fields,
                            db_path=None,
                            dry_run=False,
                            zero_missing=False,  # явно не обнулять отсутствующие позиции
                        )
                    st.success("Остатки из 1С загружены и записаны в БД")
                    st.caption(f"Всего позиций в БД: {stats.items_total} • Найдено в OData: {stats.matched_in_odata} • Обнулено отсутствующих: {stats.unmatched_zeroed}")
                    try:
                        st.json(asdict(stats))
                    except Exception:
                        st.write(stats)
                    st.rerun()
                except Exception as e:
                    st.error(f"Ошибка при выгрузке остатков: {e}")

    def compute_df(start: dt.date, days: int) -> pd.DataFrame:
        """
        Построить DataFrame по текущей структуре и сразу подтянуть сохранённые значения
        из production_plan_entries для выбранного диапазона дат (stage_id IS NULL).
        """
        days = int(days)
        df = generate_plan_dataframe(db_path=None, horizon_days=days, start_date=start)

        # Заполним сохранённые значения плана по датам
        item_codes = [str(x) for x in df.get("Артикул изделия", []) if pd.notna(x)]
        if not item_codes:
            return df

        start_iso = start.isoformat()
        end_iso = (start + dt.timedelta(days=days - 1)).isoformat()
        date_headers = [c for c in df.columns if _is_date_header(c)]
        if not date_headers:
            return df

        with get_connection(None) as conn:
            placeholders = ",".join("?" for _ in item_codes)
            rows = conn.execute(
                f"""
                SELECT i.item_code AS code, p.date AS d, p.planned_qty AS qty
                FROM production_plan_entries p
                JOIN items i ON i.item_id = p.item_id
                WHERE p.stage_id IS NULL
                  AND p.date BETWEEN ? AND ?
                  AND i.item_code IN ({placeholders})
                """,
                [start_iso, end_iso, *item_codes],
            ).fetchall()

        if rows:
            # Быстрое индексирование строк DataFrame по артикулу
            index_by_code = {str(code): idx for idx, code in enumerate(df["Артикул изделия"].tolist())}
            for r in rows:
                code = str(r["code"])
                try:
                    hdr = dt.datetime.strptime(str(r["d"]), "%Y-%m-%d").strftime("%d.%m.%y")
                except Exception:
                    continue
                if hdr not in date_headers:
                    continue
                row_idx = index_by_code.get(code)
                if row_idx is None:
                    continue
                try:
                    df.at[row_idx, hdr] = float(r["qty"])
                except Exception:
                    pass

            # Пересчёт "План на месяц" по загруженным дневным значениям
            df_numeric = df.copy()
            for c in date_headers:
                df_numeric[c] = pd.to_numeric(df_numeric[c], errors="coerce").fillna(0.0)
            df["План на месяц"] = df_numeric[date_headers].sum(axis=1)

        return df

    # Значения по умолчанию для дат/горизонта (используются на странице 'Этапы' до отрисовки формы)
    start_date = st.session_state.get("plan_start_date", dt.date.today())
    horizon_days = int(st.session_state.get("plan_horizon", 30))

    # Режим страницы "Этапы": отдельное представление, как в Excel, с подзаголовками изделий
    if page == "Этапы":
        _render_stages_page(start_date)
        return
        
    # Режим страницы "Параметры синхронизации БД"
    if page == "Параметры синхронизации БД":
        _render_sync_settings_page()
        return

    if page == "Ресурсы":
        _render_resources_page()
        return

    st.title("План производства")
    
    # Настройки параметров планирования на странице "План производства"
    with st.container():
        # Стараться выровнять кнопку по нижней кромке полей (Streamlit >= 1.31)
        try:
            col1, col2, col3 = st.columns([2, 2, 1], vertical_alignment="bottom")
            _needs_spacer = False
        except TypeError:
            # Fallback для старых версий Streamlit без vertical_alignment
            col1, col2, col3 = st.columns([2, 2, 1])
            _needs_spacer = True

        with col1:
            start_date = st.date_input("Дата начала", value=dt.date.today(), format="DD.MM.YYYY", key="plan_start_date")
        with col2:
            horizon_days = st.number_input(
                "Горизонт, дней",
                min_value=7,
                max_value=90,
                value=30,
                step=1,
                key="plan_horizon",
            )
        with col3:
            # В старых версиях Streamlit добавим небольшой отступ сверху,
            # чтобы кнопка визуально совпадала по вертикали с полями ввода.
            if _needs_spacer:
                st.markdown('<div style="height: 2.2rem;"></div>', unsafe_allow_html=True)
            recalc = st.button("Пересчитать", key="plan_recalc")

    # Пересчёт кэша по параметрам
    if recalc:
        df = compute_df(start_date, int(horizon_days))
    else:
        df = compute_df(start_date, int(horizon_days))

    # Добавление номенклатуры в план (поиск через LLM, фолбэк БД)
    with st.container():
        st.subheader("Добавить номенклатуру в план")
        q = st.text_input("Название или артикул", key="nom_search_query", placeholder="Начните вводить наименование или артикул…")
        selected_option = None
        options: list[str] = []
        option_map: dict[str, dict] = {}
        if isinstance(q, str) and len(q.strip()) >= 2:
            q_clean = q.strip()
            results = _llama_search_nomenclature(q_clean, limit=10)
            if not results:
                # Фолбэк из локальной БД
                results = _db_search_nomenclature(q_clean, limit=10)
            for r in results:
                name = str(r.get("name") or r.get("item_name") or "")
                code = str(r.get("code") or r.get("item_code") or "")
                if not code:
                    continue
                label = f"{name}, {code}" if name else code
                if label not in option_map:
                    options.append(label)
                    option_map[label] = {"name": name, "code": code}
            if options:
                selected_option = st.selectbox("Найденные варианты", options=options, index=0, key="nom_search_select")
        add_disabled = (selected_option is None)
        add_clicked = st.button("Добавить в план", type="primary", disabled=add_disabled, key="btn_add_to_plan")
        if add_clicked and selected_option:
            sel = option_map.get(selected_option, {})
            name = sel.get("name") or ""
            code = sel.get("code") or ""
            try:
                _ensure_item_and_add_to_roots(code=code, name=name)
                st.success(f"Добавлено в план: {name or ''} [{code}]")
                st.rerun()
            except Exception as e:
                st.error(f"Не удалось добавить в план: {e}")

    # Сводка по таблице
    date_cols = [c for c in df.columns if _is_date_header(c)]
    st.caption(f"Строк: {len(df)} • Колонок дат: {len(date_cols)}")

    if df.empty:
        st.info("В БД нет корневых изделий. Загрузите спецификации и повторите.")
    else:
        # Подготовка редактируемой таблицы: числовые колонки -> числа
        date_cols = [c for c in df.columns if _is_date_header(c)]
        df_for_edit = df.copy()
        for col in ("Выполнено", "Недовыполнено"):
            if col in df_for_edit.columns:
                df_for_edit[col] = pd.to_numeric(df_for_edit[col], errors="coerce").fillna(0.0)
        for c in date_cols:
            df_for_edit[c] = pd.to_numeric(df_for_edit[c], errors="coerce").fillna(0.0)

        # Конфигурация колонок
        col_config: dict[str, st.column_config.Column] = {
            "Выполнено": st.column_config.NumberColumn("Выполнено", format="%.0f", step=1, min_value=0.0),
            "Недовыполнено": st.column_config.NumberColumn("Недовыполнено", format="%.0f", step=1, min_value=0.0),
            "План на месяц": st.column_config.NumberColumn("План на месяц (сумма по дням)", format="%.0f", disabled=True),
        }
        for c in date_cols:
            d = dt.datetime.strptime(c, "%d.%m.%y").date()
            label = c
            if d.weekday() >= 5:
                # Отмечаем выходные красной меткой перед датой
                label = f"🔴 {c}"
            col_config[c] = st.column_config.NumberColumn(label=label, format="%.0f", step=1, min_value=0.0)

        edited = st.data_editor(
            df_for_edit,
            column_config=col_config,
            width="stretch",
            hide_index=True,
            key="plan_editor",
        )

        # Пересчет "План на месяц" из введенного плана по дням
        edited_numeric = edited.copy()
        for c in date_cols:
            edited_numeric[c] = pd.to_numeric(edited_numeric[c], errors="coerce").fillna(0.0)
        edited_numeric["План на месяц"] = edited_numeric[date_cols].sum(axis=1)

        st.caption("План на месяц пересчитан из введённого плана по датам.")

        # Сохранение пользовательского плана в БД (черновик, stage_id=NULL)
        if st.button("Сохранить план (черновик) в БД", type="primary"):
            saved = _save_plan_to_db(edited_numeric, date_cols)
            st.success(f"Сохранено/обновлено строк: {saved}")

        st.divider()
        st.subheader("Редактировать состав")
        edit_mode = st.checkbox("Включить режим редактирования состава", key="edit_comp_mode")
        if edit_mode:
            # Таблица выбора строк для удаления
            try:
                select_df = df[["Номенклатурное наименование изделия", "Артикул изделия"]].copy()
            except Exception:
                select_df = pd.DataFrame(columns=["Номенклатурное наименование изделия", "Артикул изделия"])
            select_df.insert(0, "Выбрать", False)
            edited_sel = st.data_editor(
                select_df,
                column_config={
                    "Выбрать": st.column_config.CheckboxColumn("Выбрать"),
                },
                hide_index=True,
                width="stretch",
                key="edit_comp_table",
            )
            # Собираем выбранные артикула
            try:
                selected_codes = [
                    str(c) for c, flag in zip(
                        edited_sel.get("Артикул изделия", []),
                        edited_sel.get("Выбрать", [])
                    ) if flag
                ]
            except Exception:
                selected_codes = []

            c1, c2 = st.columns([1, 2])
            with c1:
                confirm = st.checkbox("Подтверждаю удаление", key="confirm_del_comp")
            with c2:
                del_disabled = not selected_codes
                if st.button(f"Удалить выбранные ({len(selected_codes)})", type="secondary", disabled=del_disabled, key="btn_delete_selected"):
                    if not confirm:
                        st.warning("Поставьте галочку «Подтверждаю удаление» для продолжения.")
                    else:
                        try:
                            _delete_items_from_plan_by_codes(selected_codes)
                            st.success(f"Удалено позиций: {len(selected_codes)}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Ошибка удаления: {e}")


    # Экспорт текущего представления в CSV (для проверки/сверки)
    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Скачать CSV-представление",
        data=csv,
        file_name="production_plan_preview.csv",
        mime="text/csv",
    )


def _save_plan_to_db(df: pd.DataFrame, date_cols: list[str]) -> int:
    """
    Сохранить пользовательский план в таблицу production_plan_entries.

    Правила MVP:
      - Для каждой строки (артикул) и каждой даты записываем planned_qty.
      - stage_id не указываем (NULL), т.к. это общий план по изделию.
      - Если запись существует (уникальный ключ item_id+stage_id+date), выполняем UPSERT planned_qty.
      - completed_qty/статус на этом этапе не трогаем (оставляем как есть, по умолчанию 0/GREEN).

    Возвращает количество вставленных/обновлённых записей.
    """
    saved = 0
    # Предзагружаем соответствие item_code -> item_id
    codes = [str(x) for x in df.get("Артикул изделия", []) if pd.notna(x)]
    id_by_code: dict[str, int] = {}

    with get_connection(None) as conn:
        if codes:
            # DISTINCT коды на случай повторов
            unique_codes = sorted(set(codes))
            placeholders = ",".join("?" for _ in unique_codes)
            rows = conn.execute(
                f"SELECT item_code, item_id FROM items WHERE item_code IN ({placeholders})",
                unique_codes,
            ).fetchall()
            id_by_code = {str(r[0]): int(r[1]) for r in rows if r and r[0] is not None}

        for _, row in df.iterrows():
            code = str(row.get("Артикул изделия") or "")
            item_id = id_by_code.get(code)
            if not item_id:
                # Не нашли товар в БД — пропускаем
                continue

            for col in date_cols:
                try:
                    iso_date = dt.datetime.strptime(col, "%d.%m.%y").date().isoformat()
                except Exception:
                    continue
                # Безопасное приведение к числу
                val = row.get(col)
                try:
                    qty = float(val) if val is not None and str(val) != "" else 0.0
                except Exception:
                    qty = 0.0

                # UPDATE сначала (учитываем, что stage_id IS NULL не участвует в UNIQUE-конфликте в SQLite)
                cur = conn.execute(
                    """
                    UPDATE production_plan_entries
                       SET planned_qty = ?,
                           updated_at  = datetime('now')
                     WHERE item_id = ?
                       AND stage_id IS NULL
                       AND date = ?
                    """,
                    (qty, item_id, iso_date),
                )
                if cur.rowcount == 0:
                    # Если записи не было — вставляем новую
                    conn.execute(
                        """
                        INSERT INTO production_plan_entries (item_id, stage_id, date, planned_qty)
                        VALUES (?, NULL, ?, ?)
                        """,
                        (item_id, iso_date, qty),
                    )
                saved += 1

        conn.commit()

    return saved


def _get_stages_order(conn) -> list[str]:
    rows = conn.execute(
        "SELECT stage_name FROM production_stages ORDER BY COALESCE(stage_order, stage_id)"
    ).fetchall()
    names = [str(r[0]) for r in rows if r and r[0]]
    return names

def _get_stages_full(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT stage_id, stage_name FROM production_stages ORDER BY COALESCE(stage_order, stage_id)"
    ).fetchall()
    return [{"stage_id": int(r[0]), "stage_name": str(r[1])} for r in rows]

def _get_areas(conn) -> list:
    return conn.execute(
        "SELECT area_id, area_name, active, planning_offset_days, planning_range_days, capacity_per_day, days_per_week, hours_per_day FROM production_areas ORDER BY area_name"
    ).fetchall()

def _get_area_stage_ids(conn, area_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT stage_id FROM area_stage_map WHERE area_id = ?", (area_id,)
    ).fetchall()
    return {int(r[0]) for r in rows}

def _set_area_stages(conn, area_id: int, stage_ids: set[int]) -> None:
    current = _get_area_stage_ids(conn, area_id)
    to_delete = current - stage_ids
    to_insert = stage_ids - current
    if to_delete:
        placeholders = ",".join("?" for _ in to_delete)
        conn.execute(f"DELETE FROM area_stage_map WHERE area_id = ? AND stage_id IN ({placeholders})", [area_id, *to_delete])
    for sid in to_insert:
        conn.execute(
            "INSERT OR IGNORE INTO area_stage_map (area_id, stage_id) VALUES (?, ?)",
            (area_id, sid),
        )
    # Явно фиксируем изменения перед возможным st.rerun()
    conn.commit()

def _insert_area(conn, name: str) -> None:
    conn.execute(
        "INSERT INTO production_areas (area_name) VALUES (?)",
        (name.strip(),),
    )
    conn.commit()

def _delete_area(conn, area_id: int) -> None:
    conn.execute("DELETE FROM production_areas WHERE area_id = ?", (area_id,))
    conn.commit()

def _update_area(conn, area_id: int, active: bool, offset: int, prange: int, capacity: float, days_week: int, hours_day: float) -> None:
    conn.execute(
        """
        UPDATE production_areas
           SET active = ?,
               planning_offset_days = ?,
               planning_range_days = ?,
               capacity_per_day = ?,
               days_per_week = ?,
               hours_per_day = ?,
               updated_at = datetime('now')
         WHERE area_id = ?
        """,
        (1 if active else 0, int(offset), int(prange), float(capacity), int(days_week), float(hours_day), int(area_id)),
    )
    conn.commit()

def _render_resources_page() -> None:
    st.title("Ресурсы")
    # Покажем уведомление об успешной операции после rerun (flash)
    _notice = st.session_state.pop("resources_notice", None)
    if _notice:
        st.success(_notice)

    # Гарантируем схему БД (на случай старой базы без новых таблиц ресурсов)
    try:
        init_database()
    except Exception:
        pass
    with get_connection(None) as conn:
        # Добавление нового участка
        st.subheader("Добавить производственный участок")
        with st.form("add_area_form"):
            new_name = st.text_input("Наименование участка", key="new_area_name", placeholder="Например: Сборочный участок №1")
            submitted = st.form_submit_button("Добавить участок", type="primary")
            if submitted:
                if new_name and new_name.strip():
                    name = new_name.strip()
                    try:
                        # Предварительная проверка на дубликат по точному совпадению имени
                        try:
                            exists = conn.execute("SELECT 1 FROM production_areas WHERE area_name = ?", (name,)).fetchone()
                        except sqlite3.OperationalError:
                            # Создаём недостающие таблицы и повторяем запрос
                            init_database()
                            exists = conn.execute("SELECT 1 FROM production_areas WHERE area_name = ?", (name,)).fetchone()
                        if exists:
                            st.warning(f"Участок с именем '{name}' уже существует.")
                        else:
                            try:
                                _insert_area(conn, name)
                                st.success(f"Добавлен участок: {name}")
                                st.rerun()
                            except sqlite3.IntegrityError:
                                st.warning(f"Участок с именем '{name}' уже существует.")
                            except Exception as e:
                                st.error(f"Не удалось добавить участок: {e}")
                    except Exception as e:
                        st.error(f"Ошибка проверки наличия участка: {e}")
                else:
                    st.warning("Введите наименование участка.")

        st.divider()

        # Список участков
        try:
            areas = _get_areas(conn)
        except sqlite3.OperationalError:
            # Таблицы могли ещё не существовать в старой БД — создаём и пробуем снова
            init_database()
            areas = _get_areas(conn)
        except Exception as e:
            st.error(f"Ошибка загрузки участков: {e}")
            areas = []

        if not areas:
            st.info("Участков пока нет. Добавьте первый участок.")
            return

        # Подтянем этапы
        stages = _get_stages_full(conn)
        stage_names = [s["stage_name"] for s in stages]
        id_by_name = {s["stage_name"]: s["stage_id"] for s in stages}
        name_by_id = {s["stage_id"]: s["stage_name"] for s in stages}

        for row in areas:
            area_id = int(row["area_id"])
            area_name = str(row["area_name"])
            active = bool(row["active"])
            offset = int(row["planning_offset_days"])
            prange = int(row["planning_range_days"])
            capacity = float(row["capacity_per_day"])
            days_week = int(row["days_per_week"])
            hours_day = float(row["hours_per_day"])

            with st.expander(f"{area_name} (ID: {area_id})", expanded=False):
                top_cols = st.columns([1,1])
                with top_cols[0]:
                    if st.button("Удалить участок", key=f"del_area_{area_id}"):
                        _delete_area(conn, area_id)
                        st.success(f"Участок '{area_name}' удалён")
                        st.rerun()
                with top_cols[1]:
                    active_val = st.checkbox(
                        "Участвует в расчёте планирования",
                        value=active,
                        key=f"area_active_{area_id}"
                    )

                # Сопоставление этапов (множественный выбор)
                # Версионируем ключ виджета, чтобы после сохранения/очистки он перечитывал default из БД
                ver_key = f"area_stages_ver_{area_id}"
                if ver_key not in st.session_state:
                    st.session_state[ver_key] = 0
                ver = st.session_state[ver_key]

                selected_ids = _get_area_stage_ids(conn, area_id)
                selected_names = [name_by_id.get(sid, "") for sid in sorted(selected_ids) if sid in name_by_id]
                selected = st.multiselect(
                    "Сопоставление с этапами производства (можно выбрать несколько)",
                    options=stage_names,
                    default=selected_names,
                    key=f"area_stages_{area_id}_{ver}",
                    help="Можно выбрать несколько этапов. Выбор сохраняется после нажатия «Сохранить»."
                )

                # Отображаем текущее состояние из БД (видно, что сохранено)
                cur_ids = _get_area_stage_ids(conn, area_id)
                cur_names = [name_by_id.get(sid, str(sid)) for sid in sorted(cur_ids)]
                st.caption(f"Привязанные этапы (из БД): {', '.join(cur_names) if cur_names else '—'}")

                # Очистить все сопоставления
                if st.button("Очистить выбор", key=f"btn_clear_stages_{area_id}"):
                    try:
                        _set_area_stages(conn, area_id, set())
                        # Отобразим фактическое состояние из БД после очистки
                        cur_ids_after = _get_area_stage_ids(conn, area_id)
                        cur_names_after = [name_by_id.get(sid, str(sid)) for sid in sorted(cur_ids_after)]
                        st.success("Сопоставления этапов очищены")
                        st.caption(f"Привязанные этапы (из БД): {', '.join(cur_names_after) if cur_names_after else '—'}")
                    except Exception as e:
                        st.error(f"Не удалось очистить сопоставления: {e}")

                # Параметры планирования
                c1, c2, c3 = st.columns(3)
                with c1:
                    offset_val = st.number_input("Сдвиг планирования, дней", min_value=0, max_value=365, step=1, value=offset, key=f"area_offset_{area_id}")
                with c2:
                    range_val = st.number_input("Диапазон планирования, дней", min_value=1, max_value=365, step=1, value=prange, key=f"area_range_{area_id}")
                with c3:
                    capacity_val = st.number_input("Мощность, ед./день", min_value=0.0, step=1.0, value=capacity, key=f"area_capacity_{area_id}")

                c4, c5 = st.columns(2)
                with c4:
                    days_week_val = st.number_input("Дней в неделю", min_value=1, max_value=7, step=1, value=days_week, key=f"area_days_week_{area_id}")
                with c5:
                    hours_day_val = st.number_input("Часов в день", min_value=0.0, max_value=24.0, step=0.5, value=hours_day, key=f"area_hours_day_{area_id}")

                if st.button("Сохранить", type="primary", key=f"save_area_{area_id}"):
                    try:
                        _update_area(conn, area_id, active_val, int(offset_val), int(range_val), float(capacity_val), int(days_week_val), float(hours_day_val))
                        sel_ids = {id_by_name.get(n) for n in selected if n in id_by_name}
                        sel_ids = {sid for sid in sel_ids if sid is not None}
                        _set_area_stages(conn, area_id, sel_ids)
                        # Покажем фактически сохранённые привязки
                        cur_ids_after = _get_area_stage_ids(conn, area_id)
                        cur_names_after = [name_by_id.get(sid, str(sid)) for sid in sorted(cur_ids_after)]
                        st.success(f"Изменения сохранены. Привязанные этапы: {', '.join(cur_names_after) if cur_names_after else '—'}")
                        st.caption(f"(Сохранено в БД для участка '{area_name}')")
                    except Exception as e:
                        st.error(f"Ошибка сохранения: {e}")




def _render_stages_page(start_date: dt.date) -> None:
    """
    Страница 'Этапы' — представление как в Excel:
      - Выпадающий список этапов
      - Для каждого корневого изделия — подзаголовок и таблица его компонентов на выбранном этапе
      - Колонки соответствуют Excel-листам этапов (см. образец)
    """
    st.title("Этапы (представление как в Excel)")
    with get_connection(None) as conn:
        stage_names = _get_stages_order(conn)
        selected_stage = st.selectbox("Этап", options=stage_names, index=0, key="stages_select")

        # Остатки по всем артикулам (для колонки 'Остаток на …')
        stock_rows = conn.execute("SELECT item_code, COALESCE(stock_qty, 0.0) AS qty FROM items").fetchall()
        stock_by_code = {str(r[0]): float(r[1]) for r in stock_rows}

        # Корневые изделия
        roots_df = get_root_products(conn)

        # Заголовки таблицы как в Excel
        date_str = start_date.strftime("%d.%m.%Y")
        is_purchase = selected_stage in ("Закупные позиции", "Закупка")
        if is_purchase:
            headers = [
                "Номенклатурное наименование",
                "Артикул",
                "Количество на одно изделие",
                f"Остаток на {date_str}",
                "Минимальная партия заказа",
                "Максимальная партия заказа",
                "Заказано",
                "Срок пополнения (дни)",
            ]
        else:
            headers = [
                "Номенклатурное наименование детали/компонента",
                "Артикул детали",
                "Количество на одно изделие",
                f"Остаток на {date_str}",
                "Минимальная партия заказа",
                "Максимальная партия заказа",
                "Время пополнения (дни)",
                "В производство",
            ]

        # По каждому корневому изделию выводим подзаголовок и таблицу компонентов выбранного этапа
        for _, root in roots_df.iterrows():
            root_code = str(root.get("item_code") or "")
            root_name = str(root.get("item_name") or "")
            if not root_code:
                continue

            st.markdown(f"### {root_name} [{root_code}]")

            try:
                bom_df = explode_bom_for_root(conn, root_code=root_code, order_qty=1.0, max_depth=15)
            except Exception:
                bom_df = None

            if bom_df is None or bom_df.empty:
                st.info("Нет компонентов для этого изделия.")
                continue

            stage_filter = "Закупка" if is_purchase else selected_stage
            stage_df = bom_df[bom_df["stage_name"] == stage_filter].copy()
            if stage_df.empty:
                st.caption("Компонентов на выбранном этапе не найдено.")
                continue

            stage_df = stage_df.sort_values(["item_code"])
            rows_out = []
            for _, comp in stage_df.iterrows():
                name_v = str(comp.get("item_name") or "")
                code_v = str(comp.get("item_code") or "")
                qty_per_unit = float(comp.get("required_qty") or 0.0)
                stock_val = float(stock_by_code.get(code_v, 0.0))

                if is_purchase:
                    row = [
                        name_v,
                        code_v,
                        qty_per_unit,
                        stock_val,
                        "",  # Минимальная партия
                        "",  # Максимальная партия
                        "",  # Заказано
                        "",  # Срок пополнения (дни)
                    ]
                else:
                    row = [
                        name_v,
                        code_v,
                        qty_per_unit,
                        stock_val,
                        "",  # Минимальная партия
                        "",  # Максимальная партия
                        "",  # Время пополнения (дни)
                        "",  # В производство
                    ]
                rows_out.append(row)

            df_stage = pd.DataFrame(rows_out, columns=headers)
            st.dataframe(df_stage, width="stretch", hide_index=True)


def _render_sync_settings_page() -> None:
    """
    Страница 'Параметры синхронизации БД':
      - URL OData сервиса
      - Login
      - Password
      - Сущность/остатки (EntitySet)
      - Поля для выборки ($select, через запятую)
    Кнопки:
      - Сохранить настройки
      - Выгрузить метаданные ($metadata → output/odata_metadata.xml и summary JSON)
      - Тестировать подключение ($metadata)
    Данные сохраняются в config/odata_config.json.
    """
    st.title("Параметры синхронизации БД")

    # Загружаем текущий конфиг (толерантен к отсутствию файла/ключей)
    cfg = _load_odata_config()

    # Поля ввода
    base_url = st.text_input(
        "URL OData сервиса 1С",
        value=str(cfg.get("base_url", "")),
        help="Например: http://mtzw7/unf_demo/odata/standard.odata"
    )

    c1, c2 = st.columns(2)
    with c1:
        username = st.text_input(
            "Login",
            value=str(cfg.get("username", "") or "")
        )
    with c2:
        password = st.text_input(
            "Password",
            value=str(cfg.get("password", "") or ""),
            type="password"
        )

    entity_name = st.text_input(
        "Сущность/остатки (EntitySet)",
        value=str(cfg.get("entity_name", "") or ""),
        help="Например: AccumulationRegister_ЗапасыНаСкладах"
    )

    select_fields = st.text_input(
        "Поля для выборки ($select, через запятую)",
        value=str(cfg.get("select_fields", "") or ""),
        help="Например: Количество,Номенклатура/Код,Номенклатура/Description"
    )

    # Кнопки действий
    b1, b2, b3 = st.columns([1, 1, 1])
    with b1:
        save_click = st.button("Сохранить настройки", type="primary", key="btn_odata_save")
    with b2:
        fetch_md_click = st.button("Выгрузить метаданные", key="btn_odata_fetch_md")
    with b3:
        test_click = st.button("Тестировать подключение", key="btn_odata_test")

    # Обработка: Сохранить настройки
    if save_click:
        new_cfg = {
            "base_url": (base_url or "").strip(),
            "username": (username or "").strip(),
            "password": password or "",
            "entity_name": (entity_name or "").strip(),
            "select_fields": (select_fields or "").strip(),
        }
        _save_odata_config(new_cfg)

        # Дублируем базовые поля в session_state для сайдбара
        st.session_state.odata_url = new_cfg["base_url"]
        st.session_state.odata_entity = new_cfg["entity_name"]
        st.session_state.odata_username = new_cfg["username"]
        st.session_state.odata_password = new_cfg["password"]
        st.session_state.odata_filter = ""  # концептуально убрано, обнулим если было

        st.success("Настройки сохранены в config/odata_config.json")
        st.rerun()

    # Обработка: Выгрузить метаданные
    if fetch_md_click:
        if not base_url:
            st.warning("Укажите URL OData сервиса для выгрузки метаданных")
        else:
            try:
                import sys, subprocess
                out_xml = "output/odata_metadata.xml"
                out_json = "output/odata_metadata_summary.json"
                cmd = [
                    sys.executable,
                    "scripts/fetch_odata_metadata.py",
                    "--url", base_url,
                    "--out", out_xml,
                    "--summary-out", out_json,
                ]
                # Передаём аутентификацию (Basic), если указана
                if username:
                    cmd += ["--username", username]
                if username and password:
                    cmd += ["--password", password]
                # Выполняем
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    st.success("Метаданные выгружены")
                    if res.stdout:
                        st.text(res.stdout.strip()[:4000])
                    st.caption(f"XML: {out_xml} • Summary: {out_json}")
                else:
                    st.error("Ошибка при выгрузке метаданных")
                    st.code(res.stderr or res.stdout or "no stderr/stdout", language="text")
            except Exception as e:
                st.error(f"Сбой при выгрузке метаданных: {e}")

    # Обработка: Тестировать подключение ($metadata)
    if test_click:
        if not base_url:
            st.warning("Укажите URL OData сервиса для теста подключения")
        else:
            try:
                from src.odata_client import OData1CClient
                client = OData1CClient(
                    base_url=base_url,
                    username=username if username else None,
                    password=password if username and password else None,
                    token=None,
                )
                resp = client._make_request("$metadata")
                # Покажем краткий результат: тип контента и размер
                if isinstance(resp, dict) and "_raw" in resp:
                    raw = str(resp.get("_raw", ""))
                    ctype = str(resp.get("_content_type", ""))
                    st.success("Подключение успешно. Получен не-JSON ответ (ожидаемо для $metadata).")
                    st.caption(f"Content-Type: {ctype or 'unknown'} • size: {len(raw)} bytes")
                else:
                    # Некоторые сервера могут вернуть JSON-обёртку
                    st.success("Подключение успешно. Ответ разобран как JSON.")
                    try:
                        st.json(resp)
                    except Exception:
                        st.write(resp)
            except Exception as e:
                st.error(f"Ошибка подключения: {str(e)}")

    st.subheader("Текущие параметры")
    st.json({
        "base_url": base_url,
        "username": username,
        "password": _mask_secret(password),
        "entity_name": entity_name,
        "select_fields": select_fields,
        "config_path": str(CONFIG_PATH),
    })


if __name__ == "__main__":
    main()