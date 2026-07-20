"""Тесты чистого ядра NFP (без Frappe): арифметика и выбор источника
остатка/пополнения по природе позиции (производимая vs закупная)."""

from __future__ import annotations

from app.services.dbr.core.feeder.nfp_core import (
    bin_open_supply,
    compute_nfp,
    is_purchased,
    select_stock,
    select_supply,
)


class TestComputeNfp:
    def test_stock_plus_supply_minus_demand(self):
        assert compute_nfp(100, 30, 20) == 110.0

    def test_all_zero(self):
        assert compute_nfp(0, 0, 0) == 0.0

    def test_negative_nfp_when_demand_exceeds(self):
        # NFP может уходить в минус (спрос больше наличия + пополнения).
        assert compute_nfp(10, 0, 25) == -15.0

    def test_coerces_none_free_inputs(self):
        assert compute_nfp(5.5, 4.5, 0) == 10.0


class TestIsPurchased:
    def test_manufacture_is_produced(self):
        assert is_purchased("Manufacture") is False

    def test_purchase_is_purchased(self):
        assert is_purchased("Purchase") is True

    def test_subcontract_is_purchased(self):
        # Субподряд не производим своими руками — источники как у закупного.
        assert is_purchased("Subcontract") is True

    def test_unknown_is_purchased(self):
        # Неклассифицированное безопаснее вести как закупное: WO-пополнение
        # у него всё равно 0, а остаток по предприятию точнее одного склада.
        assert is_purchased("Unknown") is True


class TestSelectStock:
    def test_produced_uses_shelf_warehouse(self):
        # Производимая: остаток конкретного склада полки.
        assert select_stock(False, shelf_stock=40, enterprise_stock=999) == 40.0

    def test_purchased_uses_enterprise(self):
        # Закупная: остаток размазан — берём по предприятию.
        assert select_stock(True, shelf_stock=40, enterprise_stock=999) == 999.0


class TestSelectSupply:
    def test_produced_uses_work_order(self):
        assert select_supply(False, wo_supply=12, bin_supply=88) == 12.0

    def test_purchased_uses_bin(self):
        assert select_supply(True, wo_supply=12, bin_supply=88) == 88.0


class TestBinOpenSupply:
    def test_empty_is_zero(self):
        assert bin_open_supply([]) == 0.0

    def test_sums_ordered_and_indented(self):
        # Открытые PO (ordered) + открытые MR (indented) по всем строкам Bin.
        rows = [
            {"ordered_qty": 10, "indented_qty": 5},
            {"ordered_qty": 3, "indented_qty": 0},
        ]
        assert bin_open_supply(rows) == 18.0

    def test_missing_fields_default_zero(self):
        assert bin_open_supply([{"ordered_qty": 7}]) == 7.0
        assert bin_open_supply([{"indented_qty": 4}]) == 4.0
        assert bin_open_supply([{}]) == 0.0

    def test_clamps_negative_anomaly(self):
        # Отрицательная аномалия ядра не занижает пополнение.
        rows = [{"ordered_qty": -5, "indented_qty": 8}]
        assert bin_open_supply(rows) == 8.0


class TestNfpBySource:
    """Сквозной сценарий: собрать NFP из источников по природе позиции."""

    def test_produced_position_is_shelf_and_wo(self):
        purchased = is_purchased("Manufacture")
        stock = select_stock(purchased, shelf_stock=40, enterprise_stock=200)
        supply = select_supply(purchased, wo_supply=15, bin_supply=99)
        assert compute_nfp(stock, supply, demand=0) == 55.0  # 40 + 15

    def test_purchased_position_is_enterprise_and_bin(self):
        purchased = is_purchased("Purchase")
        stock = select_stock(purchased, shelf_stock=40, enterprise_stock=200)
        bin_supply = bin_open_supply(
            [{"ordered_qty": 10, "indented_qty": 5}]
        )
        supply = select_supply(purchased, wo_supply=15, bin_supply=bin_supply)
        assert compute_nfp(stock, supply, demand=0) == 215.0  # 200 + 15

    def test_purchased_with_no_open_supply(self):
        # Типичное сегодня на PROD: PO/MR ещё не заведены → supply = 0.
        purchased = is_purchased("Purchase")
        stock = select_stock(purchased, shelf_stock=0, enterprise_stock=120)
        supply = select_supply(purchased, wo_supply=0, bin_supply=bin_open_supply([]))
        assert compute_nfp(stock, supply, demand=0) == 120.0
