from datetime import date
from decimal import Decimal

from app.models import (
    DbrFeederSignal,
    DbrSettings,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    SpecComponent,
    Specification,
    Supplier,
)
from app.services.dbr.processing_trip_manifest import (
    build_manifest,
    render_manifest_html,
)


def _position(db, item):
    row = DbrSupermarketPosition(
        item_id=item.item_id,
        warehouse_ref1c="w4",
        supply_type="processing",
        mode="shelf",
        adu=1,
        commonality=1,
        rt_days=1,
        batch_days=1,
        q_batch=1,
        k_var=0,
        supply_risk_pct=0,
        red_qty=1,
        yellow_qty=1,
        green_qty=1,
        target_qty=3,
        rt_source="chain",
        data_quality=[],
        calculation_snapshot={},
    )
    db.add(row)
    db.flush()
    return row


def _signal(db, item, position, key, qty="4"):
    row = DbrFeederSignal(
        dedup_key=key,
        supermarket_position_id=position.id,
        item_id=item.item_id,
        warehouse_ref1c="w4",
        status="Open",
        suggested_qty=Decimal(qty),
        priority=1,
        need_date=date(2026, 8, 4),
        required_date=date(2026, 8, 6),
        data_quality=[],
        reason_json={},
    )
    db.add(row)
    db.flush()
    return row


def test_manifest_groups_by_contractor_and_scales_tolling_qty(db_session):
    db_session.add(DbrSettings(id=1, processing_trip_interval_days=5))
    supplier = Supplier(supplier_ref1c="supplier-1", supplier_name="Цинк")
    coated = Item(
        item_code="COATED",
        item_name="Покрытая",
        supplier_ref1c="supplier-1",
    )
    bare = Item(item_code="BARE", item_name="Голая")
    db_session.add_all([supplier, coated, bare])
    db_session.flush()
    spec = Specification(spec_name="Galvanics")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=coated.item_id, spec_id=spec.spec_id))
    db_session.add(
        SpecComponent(
            spec_id=spec.spec_id,
            item_id=bare.item_id,
            quantity=Decimal("1.5"),
            component_type="Сборка",
        )
    )
    signal = _signal(
        db_session, coated, _position(db_session, coated), "manifest-ok"
    )

    result = build_manifest(db_session)

    assert result["read_only"] is True
    assert result["processing_trip_interval_days"] == 5
    assert result["signals_total"] == 1
    assert result["contractors_total"] == 1
    line = result["contractors"][0]["lines"][0]
    assert result["contractors"][0]["contractor_name"] == "Цинк"
    assert line["signal_id"] == signal.id
    assert line["covered_item_code"] == "COATED"
    assert line["suggested_qty"] == 4
    assert line["need_date"] == "2026-08-04"
    assert line["required_date"] == "2026-08-06"
    assert line["bare_item_code"] == "BARE"
    assert line["tolling_ratio"] == 1.5
    assert line["tolling_qty"] == 6
    assert line["unresolved_reasons"] == []


def test_manifest_keeps_unresolved_signal_visible(db_session):
    item = Item(item_code="UNKNOWN", item_name="Без маршрута")
    db_session.add(item)
    db_session.flush()
    _signal(db_session, item, _position(db_session, item), "manifest-unresolved")

    result = build_manifest(db_session)

    assert result["signals_total"] == 1
    assert result["unresolved_count"] == 1
    group = result["contractors"][0]
    assert group["contractor_name"] is None
    assert group["lines"][0]["bare_item_id"] is None
    assert set(group["lines"][0]["unresolved_reasons"]) == {
        "default_specification_missing",
        "contractor_missing",
    }


def test_print_html_escapes_names():
    html = render_manifest_html(
        {
            "processing_trip_interval_days": 7,
            "contractors": [
                {
                    "contractor_ref1c": "ref",
                    "contractor_name": "<script>alert(1)</script>",
                    "lines": [
                        {
                            "covered_item_code": "A&B",
                            "covered_item_name": "<деталь>",
                            "suggested_qty": 2.0,
                            "need_date": None,
                            "required_date": None,
                            "bare_item_code": None,
                            "bare_item_name": None,
                            "tolling_qty": None,
                            "unresolved_reasons": ["bad<script>"],
                        }
                    ],
                }
            ],
        }
    )

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "A&amp;B" in html
