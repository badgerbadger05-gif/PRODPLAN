"""Tests for the DBR seed import (services/dbr/seed_import.py).

Mini TSV fixtures written to a temp dir exercise: warehouse resolution by 1C
code, assembly-rate resolution + unresolved reporting, and upsert idempotency.
"""

from app.models import (
    DbrAssemblyRate,
    DbrCategorySupplyRisk,
    DbrSettings,
    Item,
    ProductionResource,
    StockWarehouse,
)
from app.services.dbr import seed_import


# --------------------------------------------------------------------------
# Fixtures data
# --------------------------------------------------------------------------


def _seed_reference_data(db):
    db.add_all(
        [
            StockWarehouse(warehouse_ref1c="REF-092", warehouse_code="НФ-000092", warehouse_name="Склад №2"),
            StockWarehouse(warehouse_ref1c="REF-112", warehouse_code="НФ-000112", warehouse_name="Склад №3"),
            StockWarehouse(warehouse_ref1c="REF-102", warehouse_code="НФ-000102", warehouse_name="Склад №4"),
            StockWarehouse(warehouse_ref1c="REF-069", warehouse_code="НФ-000069", warehouse_name="Склад №1"),
        ]
    )
    db.add_all(
        [
            ProductionResource(resource_name="Участок сборки снегоходов"),
            ProductionResource(resource_name="Участок сборки модулей"),
        ]
    )
    db.add_all(
        [
            Item(item_code="НФ-00009114", item_name="Снегоход"),
            Item(item_code="НФ-00010435", item_name="Модуль"),
        ]
    )
    db.commit()


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


PLANNING_TSV = """field\tvalue
batch_days_turning\t10
frozen_days\t3
feeder_chain_enabled\t1
shelf_threshold_qty\t5
rt_welding_days\t15
feeder_wip_warehouse\tНФ-000092 - Склад №2 - склад механического цеха - ООО "ЗСМ"
w3_warehouse\tНФ-000112 - Склад №3 - склад покрашенных деталей - ООО "ЗСМ"
w4_warehouse\tНФ-000102 - Склад №4 - склад корпуса №2 - ООО "ЗСМ"
creation\tNULL
docstatus\t0
idx\t0
name\tProdFlow Planning Settings
owner\tAdministrator
"""

ASSEMBLY_TSV = """workstation\titem\tqty_per_capacity
Участок сборки снегоходов\tНФ-00009114\t1.000000000
Участок сборки модулей\tНФ-00010435\t10.000000000
Несуществующий участок\tНФ-00009114\t2.000000000
Участок сборки снегоходов\tНЕТ-ТАКОГО-КОДА\t2.000000000
"""

CHILD_TSV = """item_group\treceipt_warehouse\tsupply_risk_pct
Трубы круглые\tНФ-000069 - Склад №1 - склад металла - ООО "ЗСМ"\t30.000000000
Болт\tНФ-000102 - Склад №4 - склад корпуса №2 - ООО "ЗСМ"\t10.000000000
---
---
warehouse
НФ-000089 - Производственное (ИП) - ООО "ЗСМ"
НФ-000083 - Управленка - ООО "ЗСМ"
---
"""


# --------------------------------------------------------------------------
# parse_warehouse_code
# --------------------------------------------------------------------------


def test_parse_warehouse_code():
    assert seed_import.parse_warehouse_code(
        'НФ-000092 - Склад №2 - склад механического цеха - ООО "ЗСМ"'
    ) == "НФ-000092"
    assert seed_import.parse_warehouse_code("  00-000001 - Основной  ") == "00-000001"
    assert seed_import.parse_warehouse_code("") is None
    assert seed_import.parse_warehouse_code(None) is None


# --------------------------------------------------------------------------
# Planning settings
# --------------------------------------------------------------------------


def test_import_planning_settings_resolves_warehouses(db_session, tmp_path):
    _seed_reference_data(db_session)
    path = _write(tmp_path / "settings.tsv", PLANNING_TSV)

    report = seed_import.import_planning_settings(db_session, path)
    db_session.commit()

    settings = db_session.get(DbrSettings, 1)
    assert settings.batch_days_turning == 10
    assert settings.frozen_days == 3
    assert settings.feeder_chain_enabled is True
    assert int(settings.shelf_threshold_qty) == 5
    assert settings.rt_welding_days == 15
    # feeder_wip_warehouse -> w2 role
    assert settings.w2_warehouse_ref1c == "REF-092"
    assert settings.w3_warehouse_ref1c == "REF-112"
    assert settings.w4_warehouse_ref1c == "REF-102"
    # service fields ignored, no spurious skips
    assert "w2_warehouse_ref1c" in report["applied_fields"]
    assert report["warnings"] == []


def test_import_planning_settings_unresolved_warehouse_warns(db_session, tmp_path):
    # No stock warehouses seeded -> warehouse cannot resolve.
    path = _write(
        tmp_path / "settings.tsv",
        'field\tvalue\nfrozen_days\t4\nw3_warehouse\tНФ-999999 - Missing - ООО\n',
    )
    report = seed_import.import_planning_settings(db_session, path)
    db_session.commit()

    settings = db_session.get(DbrSettings, 1)
    assert settings.frozen_days == 4
    assert settings.w3_warehouse_ref1c is None
    assert any("НФ-999999" in w for w in report["warnings"])


def test_import_planning_settings_idempotent(db_session, tmp_path):
    _seed_reference_data(db_session)
    path = _write(tmp_path / "settings.tsv", PLANNING_TSV)
    seed_import.import_planning_settings(db_session, path)
    db_session.commit()
    seed_import.import_planning_settings(db_session, path)
    db_session.commit()
    # still a single settings row
    assert db_session.query(DbrSettings).count() == 1


# --------------------------------------------------------------------------
# Assembly rates
# --------------------------------------------------------------------------


def test_import_assembly_rates_resolves_and_reports_unresolved(db_session, tmp_path):
    _seed_reference_data(db_session)
    path = _write(tmp_path / "rates.tsv", ASSEMBLY_TSV)

    report = seed_import.import_assembly_rates(db_session, path)
    db_session.commit()

    assert report["loaded"] == 2
    assert report["updated"] == 0
    assert report["unresolved_count"] == 2
    reasons = " ".join(u["reason"] for u in report["unresolved"])
    assert "production_resources" in reasons
    assert "items" in reasons

    rates = db_session.query(DbrAssemblyRate).all()
    assert len(rates) == 2


def test_import_assembly_rates_idempotent_upsert(db_session, tmp_path):
    _seed_reference_data(db_session)
    path = _write(tmp_path / "rates.tsv", ASSEMBLY_TSV)

    seed_import.import_assembly_rates(db_session, path)
    db_session.commit()
    report2 = seed_import.import_assembly_rates(db_session, path)
    db_session.commit()

    # second run updates the two resolved rows, inserts nothing
    assert report2["loaded"] == 0
    assert report2["updated"] == 2
    assert db_session.query(DbrAssemblyRate).count() == 2


def test_import_assembly_rates_updates_qty(db_session, tmp_path):
    _seed_reference_data(db_session)
    _write(tmp_path / "rates.tsv", ASSEMBLY_TSV)
    seed_import.import_assembly_rates(db_session, tmp_path / "rates.tsv")
    db_session.commit()

    # change qty for the snegohod row and re-import
    _write(
        tmp_path / "rates2.tsv",
        "workstation\titem\tqty_per_capacity\nУчасток сборки снегоходов\tНФ-00009114\t9.000\n",
    )
    seed_import.import_assembly_rates(db_session, tmp_path / "rates2.tsv")
    db_session.commit()

    snow_res = (
        db_session.query(ProductionResource)
        .filter_by(resource_name="Участок сборки снегоходов")
        .one()
    )
    snow_item = db_session.query(Item).filter_by(item_code="НФ-00009114").one()
    rate = (
        db_session.query(DbrAssemblyRate)
        .filter_by(resource_id=snow_res.resource_id, item_id=snow_item.item_id)
        .one()
    )
    assert int(rate.qty_per_capacity) == 9


# --------------------------------------------------------------------------
# Category supply-risk (first block only)
# --------------------------------------------------------------------------


def test_import_category_risks_first_block_only(db_session, tmp_path):
    _seed_reference_data(db_session)
    path = _write(tmp_path / "child.tsv", CHILD_TSV)

    report = seed_import.import_category_risks(db_session, path)
    db_session.commit()

    rows = {r.item_group: r for r in db_session.query(DbrCategorySupplyRisk).all()}
    # only the two category-risk rows; ignored-warehouse block skipped
    assert set(rows) == {"Трубы круглые", "Болт"}
    assert rows["Трубы круглые"].receipt_warehouse_ref1c == "REF-069"
    assert int(rows["Трубы круглые"].supply_risk_pct) == 30
    assert rows["Болт"].receipt_warehouse_ref1c == "REF-102"
    assert report["loaded"] == 2


def test_import_category_risks_idempotent(db_session, tmp_path):
    _seed_reference_data(db_session)
    path = _write(tmp_path / "child.tsv", CHILD_TSV)
    seed_import.import_category_risks(db_session, path)
    db_session.commit()
    report2 = seed_import.import_category_risks(db_session, path)
    db_session.commit()
    assert report2["loaded"] == 0
    assert report2["updated"] == 2
    assert db_session.query(DbrCategorySupplyRisk).count() == 2


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def test_import_all(db_session, tmp_path):
    _seed_reference_data(db_session)
    _write(tmp_path / "erpnext_planning_settings.tsv", PLANNING_TSV)
    _write(tmp_path / "erpnext_assembly_rates.tsv", ASSEMBLY_TSV)
    _write(tmp_path / "erpnext_child_settings.tsv", CHILD_TSV)

    summary = seed_import.import_all(db_session, tmp_path)
    db_session.commit()

    assert summary["assembly_rates"]["loaded"] == 2
    assert summary["category_risks"]["loaded"] == 2
    assert db_session.get(DbrSettings, 1).w2_warehouse_ref1c == "REF-092"
