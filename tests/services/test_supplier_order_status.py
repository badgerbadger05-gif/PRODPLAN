"""Тесты канонической модели состояний заказа поставщику (1С) → фазы."""
import pytest

from app.services.supplier_order_status import (
    SupplyPhase,
    normalize_state,
    phase_for_state,
    phase_value,
    state_counts_in_mrp,
    state_is_terminal,
)


@pytest.mark.parametrize(
    "state,expected",
    [
        ("Новый заказ", SupplyPhase.NO_GOODS),
        ("В закупку", SupplyPhase.NO_GOODS),
        ("Бухгалтерия", SupplyPhase.NO_GOODS),
        ("Оплачен частично", SupplyPhase.IN_TRANSIT),
        ("Оплачен полностью", SupplyPhase.IN_TRANSIT),
        ("Оплаченый полностью", SupplyPhase.IN_TRANSIT),  # опечатка в 1С
        ("Заказан (товар в пути)", SupplyPhase.IN_TRANSIT),
        ("Товар в пути", SupplyPhase.IN_TRANSIT),
        ("В пути", SupplyPhase.IN_TRANSIT),
        ("Получен Агентом", SupplyPhase.IN_TRANSIT),
        ("Отправлен по России", SupplyPhase.IN_TRANSIT),
        ("Принят на склад", SupplyPhase.IN_STOCK),
        ("Отменен", SupplyPhase.TERMINAL),
        ("Отменён", SupplyPhase.TERMINAL),  # с ё
        ("Завершен", SupplyPhase.TERMINAL),
        ("Завершён успешно", SupplyPhase.TERMINAL),
    ],
)
def test_phase_for_state(state, expected):
    assert phase_for_state(state) is expected


def test_normalize_state_casefold_and_yo():
    assert normalize_state("  Оплачён ПОЛНОСТЬЮ ") == "оплачен полностью"
    assert normalize_state(None) == ""


@pytest.mark.parametrize(
    "state,counts",
    [
        ("В пути", True),
        ("Заказан (товар в пути)", True),
        ("Товар в пути", True),
        ("Принят на склад", True),
        ("Оплаченый полностью", True),
        ("В закупку", False),
        ("Новый заказ", False),
        ("Бухгалтерия", False),
        ("Завершен", False),
        ("Отменен", False),
        (None, False),
        ("какое-то новое состояние", False),
    ],
)
def test_state_counts_in_mrp(state, counts):
    assert state_counts_in_mrp(state) is counts


@pytest.mark.parametrize(
    "state,terminal",
    [
        ("Завершен", True),
        ("Отменён", True),
        ("Завершён успешно", True),
        ("В закупку", False),
        ("Новый заказ", False),
        ("Бухгалтерия", False),
        ("В пути", False),
        ("Принят на склад", False),
        (None, False),
    ],
)
def test_state_is_terminal(state, terminal):
    assert state_is_terminal(state) is terminal


def test_unknown_state_is_unknown_phase():
    assert phase_for_state("совершенно новое") is SupplyPhase.UNKNOWN
    assert phase_value("совершенно новое") == "unknown"
    assert phase_value(None) == "unknown"


def test_phase_value_serialization():
    assert phase_value("В пути") == "in_transit"
    assert phase_value("Бухгалтерия") == "no_goods"
    assert phase_value("Принят на склад") == "in_stock"
    assert phase_value("Завершен") == "terminal"
