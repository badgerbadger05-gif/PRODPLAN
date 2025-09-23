from __future__ import annotations

import threading
import time
from typing import Dict, Any


class ProgressManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = {}

    def start(self, key: str, total: int | None = None, message: str = "") -> None:
        with self._lock:
            self._state[key] = {
                "started_at": time.time(),
                "finished_at": None,
                "finished": False,
                "error": None,
                "total": int(total or 0),
                "processed": 0,
                "percent": 0.0,
                "message": message or "Запуск...",
            }

    def update(self, key: str, processed: int | None = None, total: int | None = None, message: str | None = None) -> None:
        with self._lock:
            st = self._state.get(key)
            if not st:
                # ленивый старт, если не был вызван явно
                st = {
                    "started_at": time.time(),
                    "finished_at": None,
                    "finished": False,
                    "error": None,
                    "total": 0,
                    "processed": 0,
                    "percent": 0.0,
                    "message": "Инициализация...",
                }
                self._state[key] = st
            if total is not None:
                st["total"] = max(0, int(total))
            if processed is not None:
                st["processed"] = max(0, int(processed))
            t = st.get("total") or 0
            p = st.get("processed") or 0
            if t > 0:
                st["percent"] = max(0.0, min(1.0, p / float(t)))
            else:
                # если неизвестен total — оценочно
                st["percent"] = 0.0
            if message is not None:
                st["message"] = message

    def finish(self, key: str, error: str | None = None, message: str | None = None) -> None:
        with self._lock:
            st = self._state.get(key)
            if not st:
                st = {}
                self._state[key] = st
            st["finished"] = True
            st["finished_at"] = time.time()
            st["error"] = error
            if message is not None:
                st["message"] = message
            # если нет ошибки и известен total — выставим 100%
            if not error:
                t = st.get("total") or 0
                p = st.get("processed") or 0
                if t > 0 and p < t:
                    st["processed"] = t
                st["percent"] = 1.0 if not error else st.get("percent", 0.0)

    def get_state(self, key: str) -> Dict[str, Any]:
        with self._lock:
            st = self._state.get(key)
            if not st:
                return {
                    "started_at": None,
                    "finished_at": None,
                    "finished": False,
                    "error": None,
                    "total": 0,
                    "processed": 0,
                    "percent": 0.0,
                    "message": "",
                }
            return dict(st)


progress = ProgressManager()