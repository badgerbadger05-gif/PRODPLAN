"""Read-only trip manifest preview for the toll-processing feeder."""

from __future__ import annotations

from html import escape
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ...models import (
    DbrFeederSignal,
    DbrSettings,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    SpecComponent,
    Specification,
    Supplier,
)

DEFAULT_TRIP_INTERVAL_DAYS = 7


def _assembly_component(
    db: Session, item_id: int
) -> tuple[Item | None, float | None, list[str]]:
    default_specs = (
        db.query(Specification)
        .join(
            DefaultSpecification,
            DefaultSpecification.spec_id == Specification.spec_id,
        )
        .filter(DefaultSpecification.item_id == item_id)
        .order_by(DefaultSpecification.id.asc())
        .all()
    )
    if not default_specs:
        return None, None, ["default_specification_missing"]
    if len(default_specs) != 1:
        return None, None, ["multiple_default_specifications"]

    rows = (
        db.query(SpecComponent, Item)
        .join(Item, Item.item_id == SpecComponent.item_id)
        .filter(
            SpecComponent.spec_id == default_specs[0].spec_id,
            SpecComponent.component_type == "Сборка",
        )
        .order_by(SpecComponent.component_id.asc())
        .all()
    )
    if len(rows) != 1:
        return None, None, ["assembly_component_count_not_one"]
    component, item = rows[0]
    ratio = float(component.quantity or 0)
    if ratio <= 0:
        return item, None, ["assembly_component_qty_not_positive"]
    return item, ratio, []


def build_manifest(db: Session) -> dict[str, Any]:
    """Group active processing signals by the contractor configured on Item."""
    settings = db.get(DbrSettings, 1)
    interval = (
        int(settings.processing_trip_interval_days)
        if settings is not None
        else DEFAULT_TRIP_INTERVAL_DAYS
    )
    signals = (
        db.query(DbrFeederSignal)
        .options(joinedload(DbrFeederSignal.item))
        .join(
            DbrSupermarketPosition,
            DbrSupermarketPosition.id == DbrFeederSignal.supermarket_position_id,
        )
        .filter(
            DbrFeederSignal.status == "Open",
            DbrSupermarketPosition.supply_type == "processing",
            DbrSupermarketPosition.is_active.is_(True),
        )
        .order_by(
            DbrFeederSignal.need_date.asc(),
            DbrFeederSignal.required_date.asc(),
            DbrFeederSignal.priority.desc(),
            DbrFeederSignal.id.asc(),
        )
        .all()
    )
    suppliers = {
        str(row.supplier_ref1c or "").strip().casefold(): row
        for row in db.query(Supplier)
        .filter(
            Supplier.supplier_ref1c.is_not(None),
            func.trim(Supplier.supplier_ref1c) != "",
        )
        .all()
    }
    groups: dict[str, dict[str, Any]] = {}
    unresolved_count = 0
    for signal in signals:
        covered = signal.item
        supplier_ref = str(covered.supplier_ref1c or "").strip()
        supplier = suppliers.get(supplier_ref.casefold())
        group_key = supplier_ref.casefold() or "__unresolved__"
        group = groups.setdefault(
            group_key,
            {
                "contractor_ref1c": supplier_ref or None,
                "contractor_name": (
                    str(supplier.supplier_name or "") if supplier is not None else None
                ),
                "lines": [],
            },
        )
        bare, ratio, reasons = _assembly_component(db, int(signal.item_id))
        if not supplier_ref:
            reasons.append("contractor_missing")
        elif supplier is None:
            reasons.append("contractor_not_synced")
        qty = float(signal.suggested_qty or 0)
        if qty <= 0:
            reasons.append("suggested_qty_not_positive")
        if signal.is_incomplete:
            reasons.append("signal_incomplete")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            unresolved_count += 1
        group["lines"].append(
            {
                "signal_id": int(signal.id),
                "covered_item_id": int(covered.item_id),
                "covered_item_code": str(covered.item_code or ""),
                "covered_item_name": str(covered.item_name or ""),
                "suggested_qty": qty,
                "need_date": signal.need_date.isoformat() if signal.need_date else None,
                "required_date": (
                    signal.required_date.isoformat()
                    if signal.required_date
                    else None
                ),
                "bare_item_id": int(bare.item_id) if bare is not None else None,
                "bare_item_code": str(bare.item_code or "") if bare is not None else None,
                "bare_item_name": str(bare.item_name or "") if bare is not None else None,
                "tolling_ratio": ratio,
                "tolling_qty": round(qty * ratio, 4) if ratio is not None else None,
                "unresolved_reasons": reasons,
            }
        )

    ordered = sorted(
        groups.values(),
        key=lambda row: (
            row["contractor_name"] is None,
            str(row["contractor_name"] or "").casefold(),
            str(row["contractor_ref1c"] or ""),
        ),
    )
    return {
        "read_only": True,
        "processing_trip_interval_days": interval,
        "signals_total": len(signals),
        "contractors_total": len(ordered),
        "unresolved_count": unresolved_count,
        "contractors": ordered,
    }


def render_manifest_html(manifest: dict[str, Any]) -> str:
    """Render a deliberately plain browser-printable document."""
    parts = [
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">",
        "<title>Рейс на переработку</title>",
        "<style>body{font:14px sans-serif}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #777;padding:5px;text-align:left}"
        ".warn{color:#a00}@media print{button{display:none}}</style></head><body>",
        "<button onclick=\"window.print()\">Печать</button>",
        "<h1>Рейс на переработку</h1>",
        f"<p>Интервал рейса: {int(manifest['processing_trip_interval_days'])} дн.</p>",
    ]
    for contractor in manifest["contractors"]:
        title = contractor["contractor_name"] or "Подрядчик не определён"
        ref = contractor["contractor_ref1c"] or ""
        parts.append(f"<h2>{escape(str(title))}</h2><p>{escape(str(ref))}</p>")
        parts.append(
            "<table><thead><tr><th>Покрытая деталь</th><th>Кол-во</th>"
            "<th>Нужно</th><th>Требуется</th><th>Голая деталь</th>"
            "<th>Давальческое кол-во</th><th>Проблемы</th></tr></thead><tbody>"
        )
        for line in contractor["lines"]:
            issues = ", ".join(line["unresolved_reasons"])
            issue_class = " class=\"warn\"" if issues else ""
            parts.append(
                "<tr>"
                f"<td>{escape(line['covered_item_code'])} — "
                f"{escape(line['covered_item_name'])}</td>"
                f"<td>{line['suggested_qty']:g}</td>"
                f"<td>{escape(str(line['need_date'] or ''))}</td>"
                f"<td>{escape(str(line['required_date'] or ''))}</td>"
                f"<td>{escape(str(line['bare_item_code'] or ''))} — "
                f"{escape(str(line['bare_item_name'] or ''))}</td>"
                f"<td>{'' if line['tolling_qty'] is None else format(line['tolling_qty'], 'g')}</td>"
                f"<td{issue_class}>{escape(issues)}</td></tr>"
            )
        parts.append("</tbody></table>")
    parts.append("</body></html>")
    return "".join(parts)
