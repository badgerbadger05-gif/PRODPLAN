from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from ..models import ProductionManufacture, ProductionMaterialIssue, ProductionOrder


def chain_key_for_order(order: ProductionOrder) -> str:
    run_part = (int(order.source_run_id) if order.source_run_id is not None else 0) % 10000
    order_part = int(order.order_id) % 100000
    return f"{run_part:04d}{order_part:05d}"


def production_order_number(order: ProductionOrder) -> str:
    return f"PP{chain_key_for_order(order)}"


def purchase_order_number(run_id: int, index: int) -> str:
    return f"PO{int(run_id) % 100000:05d}{int(index) % 1000:03d}"


def suffix_for_index(index: int) -> str:
    if index <= 0:
        index = 1
    letters = []
    value = int(index)
    while value:
        value, rem = divmod(value - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


def material_issue_suffix(db: Session, issue: ProductionMaterialIssue) -> str:
    rows = (
        db.query(ProductionMaterialIssue.issue_id)
        .filter(
            ProductionMaterialIssue.order_id == int(issue.order_id),
            ProductionMaterialIssue.direction == str(issue.direction or "issue"),
            ProductionMaterialIssue.status != "cancelled",
        )
        .order_by(ProductionMaterialIssue.issue_id.asc())
        .all()
    )
    ids = [int(row[0]) for row in rows]
    try:
        idx = ids.index(int(issue.issue_id)) + 1
    except ValueError:
        idx = len(ids) + 1
    return suffix_for_index(idx)


def material_issue_number(db: Session, issue: ProductionMaterialIssue) -> str:
    prefix = "RT" if str(issue.direction or "issue") == "return" else "MT"
    # 1C's Document_ПеремещениеЗапасов.Number is limited to 11 characters in
    # the target base. Order-chain suffixes like MT001204813A get truncated by
    # 1C to MT001204813, making A/B documents collide. Keep the whole number
    # within 11 chars and use issue_id for uniqueness.
    return f"{prefix}{int(issue.issue_id) % 1_000_000_000:09d}"


def manufacture_suffix(db: Session, manufacture: ProductionManufacture) -> str:
    rows = (
        db.query(ProductionManufacture.manufacture_id)
        .filter(
            ProductionManufacture.order_id == int(manufacture.order_id),
            ProductionManufacture.status != "cancelled",
        )
        .order_by(ProductionManufacture.manufacture_id.asc())
        .all()
    )
    ids = [int(row[0]) for row in rows]
    if len(ids) <= 1:
        return ""
    try:
        idx = ids.index(int(manufacture.manufacture_id)) + 1
    except ValueError:
        idx = len(ids) + 1
    return suffix_for_index(idx)


def manufacture_number(db: Session, manufacture: ProductionManufacture) -> str:
    order = manufacture.order
    if order is None:
        order = db.query(ProductionOrder).filter(ProductionOrder.order_id == int(manufacture.order_id)).one()
    return f"MF{chain_key_for_order(order)}{manufacture_suffix(db, manufacture)}"


def piecework_number(db: Session, manufacture: ProductionManufacture) -> str:
    order = manufacture.order
    if order is None:
        order = db.query(ProductionOrder).filter(ProductionOrder.order_id == int(manufacture.order_id)).one()
    return f"PW{chain_key_for_order(order)}{manufacture_suffix(db, manufacture)}"
