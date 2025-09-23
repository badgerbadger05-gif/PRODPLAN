from __future__ import annotations

from typing import Any, Dict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.stages_calculator import calculate_stages

router = APIRouter(prefix="/v1/stages", tags=["stages"])


@router.post("/calculate")
def calculate(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Рассчитать этапы производства.

    Алгоритм:
    - Берёт все изделия из строк плана (root_products)
    - Находит для них спецификации по умолчанию
    - Рекурсивно разворачивает состав (учитывая множители родителей)
    - Группирует по этапам появления (stage_id из строк состава)
    - Оставляет только детали с методом пополнения 'Производство'
    - Возвращает структуру: этап -> изделия -> детали

    Возврат:
    {
      "asOf": "ISO-время последней синхронизации остатков или null",
      "stages": [
        {
          "stage_id": int,
          "stage_name": str,
          "products": [
            {
              "root_item_id": int,
              "root_item_code": str,
              "root_item_name": str,
              "components": [
                {
                  "item_id": int,
                  "item_code": str,
                  "item_name": str,
                  "qty_per_unit": float,
                  "stock_qty": float,
                  "replenishment_method": str | null,
                  "min_batch": float | null,
                  "max_batch": float | null
                }
              ]
            }
          ]
        }
      ]
    }
    """
    try:
        return calculate_stages(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stages calculation error: {e}")