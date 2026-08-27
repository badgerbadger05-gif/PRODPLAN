from __future__ import annotations

import os

from sqlalchemy.orm import Session

from ..models import ProductionMaterialIssue, ProductionManufacture, ProductionOrder


_MATERIAL_ISSUE_NUMBER_MODULUS = 1_000_000_000


def material_issue_number_value(
    issue_id: int,
    *,
    direction: str = "issue",
    probe_offset: int = 0,
) -> str:
    """Return a contour-partitioned 11-character stock-transfer number."""
    raw_offset = str(os.getenv("PRODPLAN_MATERIAL_ISSUE_NUMBER_OFFSET", "0")).strip()
    try:
        contour_offset = int(raw_offset)
    except ValueError as exc:
        raise RuntimeError(
            "PRODPLAN_MATERIAL_ISSUE_NUMBER_OFFSET must be an integer"
        ) from exc
    if not 0 <= contour_offset < _MATERIAL_ISSUE_NUMBER_MODULUS:
        raise RuntimeError(
            "PRODPLAN_MATERIAL_ISSUE_NUMBER_OFFSET must be between 0 and 999999999"
        )
    prefix = "RT" if str(direction or "issue") == "return" else "MT"
    number_part = (
        contour_offset + int(issue_id) + int(probe_offset)
    ) % _MATERIAL_ISSUE_NUMBER_MODULUS
    return f"{prefix}{number_part:09d}"


def chain_key_for_order(order: ProductionOrder) -> str:
    run_part = (int(order.source_run_id) if order.source_run_id is not None else 0) % 10000
    order_part = int(order.order_id) % 100000
    return f"{run_part:04d}{order_part:05d}"


def production_order_number(order: ProductionOrder) -> str:
    return f"PP{chain_key_for_order(order)}"


def purchase_order_number(run_id: int, index: int) -> str:
    return f"PO{int(run_id) % 100000:05d}{int(index) % 1000:03d}"


def material_issue_number(db: Session, issue: ProductionMaterialIssue) -> str:
    # 1C's Document_ПеремещениеЗапасов.Number is limited to 11 characters in
    # the target base. Order-chain suffixes like MT001204813A get truncated by
    # 1C to MT001204813, making A/B documents collide. Keep the whole number
    # within 11 chars. Parallel contours use disjoint configured ranges.
    return material_issue_number_value(
        int(issue.issue_id),
        direction=str(issue.direction or "issue"),
    )


def manufacture_number(db: Session, manufacture: ProductionManufacture) -> str:
    """Return stable, bounded number for Document_СборкаЗапасов."""
    return f"MF{int(manufacture.manufacture_id) % 1_000_000_000:09d}"


def piecework_number(db: Session, manufacture: ProductionManufacture) -> str:
    """Return stable, bounded number for Document_СдельныйНаряд."""
    return f"PW{int(manufacture.manufacture_id) % 1_000_000_000:09d}"
