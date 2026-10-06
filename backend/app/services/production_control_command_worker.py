"""Bounded command worker: prepare materials and publish local MAKE changes.

HTTP readers remain read-only. The subprocess owns calculation, persistence,
and the whole local-command transaction; it never exports documents to 1C.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder


def run_command(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", __name__],
            input=json.dumps({"action": action, "arguments": arguments}),
            text=True, capture_output=True, timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(503, detail="Подготовка данных заняла слишком много времени. Обновите журнал.") from exc
    lines = [line[len("COMMAND_RESULT "):] for line in result.stdout.splitlines()
             if line.startswith("COMMAND_RESULT ")]
    if not lines:
        raise HTTPException(503, detail="Воркер подготовки данных временно недоступен")
    envelope = json.loads(lines[-1])
    if not envelope["ok"]:
        raise HTTPException(envelope["status"], detail=envelope["detail"])
    return envelope["result"]


def _execute(db, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if action == "materials":
        from .production_control_journal_projection import prepare_current_work_materials
        result = prepare_current_work_materials(db, **arguments)
    elif action == "materialize":
        from app.routers.production_control import OrdersFromWorkItemsPayload, _materialize_orders_from_work_items
        result = _materialize_orders_from_work_items(OrdersFromWorkItemsPayload.model_validate(arguments), db)
    elif action == "repair":
        from app import models
        from .production_control_journal_projection import publish_local_make_changes
        db.query(models.PlanningTruthState).filter_by(id=1).with_for_update().one()
        result = {"source_revision": publish_local_make_changes(db, arguments["work_item_ids"])}
    else:
        raise ValueError("Неизвестная команда подготовки данных")
    db.commit()
    return result


def execute(db, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from .item_ledger.physical_refresh_orchestrator import _acquire_lifecycle_lock, _release_lifecycle_lock
    lock = _acquire_lifecycle_lock(db)
    if not lock:
        raise HTTPException(409, detail={"code": "production_ledger_refresh_busy",
            "message": "Сейчас обновляется Ledger. Дождитесь завершения обновления и повторите действие."})
    try:
        return _execute(db, action, arguments)
    except Exception:
        db.rollback()
        raise
    finally:
        _release_lifecycle_lock(lock)


def main() -> None:
    from app.database import SessionLocal
    from .item_ledger.current_execution import CurrentExecutionUnavailable
    from .planning_truth import PlanningTruthUnavailable

    command = json.load(sys.stdin)
    with SessionLocal() as db:
        try:
            result = execute(db, command["action"], command["arguments"])
            envelope = {"ok": True, "result": result}
        except HTTPException as exc:
            db.rollback()
            envelope = {"ok": False, "status": exc.status_code, "detail": exc.detail}
        except (CurrentExecutionUnavailable, PlanningTruthUnavailable) as exc:
            db.rollback()
            envelope = {"ok": False, "status": 503, "detail": str(exc)}
        except Exception as exc:
            db.rollback()
            envelope = {"ok": False, "status": 400, "detail": str(exc)}
    print("COMMAND_RESULT " + json.dumps(jsonable_encoder(envelope), ensure_ascii=False))


if __name__ == "__main__":
    main()
