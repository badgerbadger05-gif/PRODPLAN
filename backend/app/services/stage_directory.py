"""Справочник производственных этапов.

Единственная живая часть удалённого ``plan_service``: плоский список этапов
для выпадающих списков UI. Легаси-матрица плана (``production_plan_entries`` /
``root_products``) удалена — плановый выпуск ведёт только канонический
периодный план (``production_plan_header`` / ``production_plan_line``).
"""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..models import ProductionStage


def fetch_stages(db: Session) -> List[Dict[str, Any]]:
    """Список этапов производства: ``[{'value': stage_id, 'label': stage_name}, ...]``."""
    stages = (
        db.query(ProductionStage)
        .order_by(
            ProductionStage.stage_order.asc().nulls_last(),
            ProductionStage.stage_name,
        )
        .all()
    )

    return [
        {"value": int(stage.stage_id), "label": str(stage.stage_name)}
        for stage in stages
    ]
