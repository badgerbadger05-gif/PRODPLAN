from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import ProductionMaterialIssue, ProductionManufacture, ProductionOrder


def chain_key_for_order(order: ProductionOrder) -> str:
    run_part = (int(order.source_run_id) if order.source_run_id is not None else 0) % 10000
    order_part = int(order.order_id) % 100000
    return f"{run_part:04d}{order_part:05d}"


def production_order_number(order: ProductionOrder) -> str:
    return f"PP{chain_key_for_order(order)}"


def purchase_order_number(run_id: int, index: int) -> str:
    return f"PO{int(run_id) % 100000:05d}{int(index) % 1000:03d}"


def material_issue_number(db: Session, issue: ProductionMaterialIssue) -> str:
    direction = str(issue.direction or "issue")
    # Local workshop-stock reservation keeps the same movement series; only true
    # returns are exported with RT.
    prefix = {"return": "RT"}.get(direction, "MT")
    # 1C's Document_ПеремещениеЗапасов.Number is limited to 11 characters in
    # the target base. Order-chain suffixes like MT001204813A get truncated by
    # 1C to MT001204813, making A/B documents collide. Keep the whole number
    # within 11 chars and use issue_id for uniqueness.
    return f"{prefix}{int(issue.issue_id) % 1_000_000_000:09d}"


def manufacture_number(db: Session, manufacture: ProductionManufacture) -> str:
    """Return stable, bounded number for Document_СборкаЗапасов."""
    return f"MF{int(manufacture.manufacture_id) % 1_000_000_000:09d}"


def piecework_number(db: Session, manufacture: ProductionManufacture) -> str:
    """Return stable, bounded number for Document_СдельныйНаряд."""
    return f"PW{int(manufacture.manufacture_id) % 1_000_000_000:09d}"
