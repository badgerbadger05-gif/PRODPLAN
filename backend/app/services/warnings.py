from __future__ import annotations

import json
import logging
from typing import Any, Dict


def make_warning(code: str, msg: str, **context: Any) -> Dict[str, Any]:
    """
    Create a standardized warning dict.
    Fields:
      - code: str
      - msg: str
      - ...context: optional fields like run_id, order_id, item_id, production_kind_id, spec_id, date(s), from_qty, to_qty, residual_hours, etc.
    """
    w: Dict[str, Any] = {"code": str(code), "msg": str(msg)}
    if context:
        for k, v in context.items():
            w[k] = v
    return w


def log_warning(logger: logging.Logger, code: str, msg: str, **context: Any) -> Dict[str, Any]:
    """
    Create a standardized warning dict and emit it via logger.warning in JSON format.
    Returns the dict to allow appending to collections.
    """
    w = make_warning(code, msg, **context)
    try:
        # Emit as JSON for structured logs
        logger.warning(json.dumps(w, ensure_ascii=False))
    except Exception:
        # Fallback: as plain dict string
        logger.warning(str(w))
    return w