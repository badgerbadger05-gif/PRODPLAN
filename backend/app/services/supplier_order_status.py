"""
Каноническая модель состояний заказа поставщику (1С) и их группировка по
фазам движения товара.

Состояния приходят из 1С (`Catalog_СостоянияЗаказовПоставщикам`,
поле `SupplierOrder.order_state_name`). Снабжение формализовало 9 рабочих
состояний, сгруппированных в 3 фазы:

    «Нет товара»                 → Новый заказ · В закупку · Бухгалтерия
    «Товар в пути / в произв.»   → Оплачен частично · Оплачен полностью ·
                                   Заказан (товар в пути) · В пути ·
                                   Получен Агентом · Отправлен по России
    «Товар на складе»            → Принят на склад

Плюс терминальные состояния вне модели снабжения, но реально присутствующие
в 1С: Отменён · Завершён · Завершён успешно.

Правило учёта в MRP (deny-by-default): потребность уменьшают (нетуют) только
фазы IN_TRANSIT и IN_STOCK. Любое незамапленное состояние трактуется как
UNKNOWN и НЕ нетует — это сознательно консервативно (риск пере-заказа, а не
недозаказа). Незнакомые состояния логируются, чтобы вовремя расширить карту.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Set

logger = logging.getLogger(__name__)

# Состояния, для которых уже залогировали предупреждение (чтобы не спамить).
_warned_unknown_states: Set[str] = set()


class SupplyPhase(str, Enum):
    NO_GOODS = "no_goods"       # Нет товара
    IN_TRANSIT = "in_transit"   # Товар в пути / в производстве
    IN_STOCK = "in_stock"       # Товар на складе
    TERMINAL = "terminal"       # Отменён / Завершён
    UNKNOWN = "unknown"         # незамапленное состояние 1С


# Нормализованное имя состояния 1С → фаза.
# Нормализация: strip().casefold().replace("ё", "е").
STATE_TO_PHASE = {
    "новый заказ": SupplyPhase.NO_GOODS,
    "в закупку": SupplyPhase.NO_GOODS,
    "бухгалтерия": SupplyPhase.NO_GOODS,
    "оплачен частично": SupplyPhase.IN_TRANSIT,
    "оплачен полностью": SupplyPhase.IN_TRANSIT,
    "оплаченый полностью": SupplyPhase.IN_TRANSIT,  # опечатка в справочнике 1С
    "заказан (товар в пути)": SupplyPhase.IN_TRANSIT,
    "в пути": SupplyPhase.IN_TRANSIT,
    "получен агентом": SupplyPhase.IN_TRANSIT,
    "отправлен по россии": SupplyPhase.IN_TRANSIT,
    "принят на склад": SupplyPhase.IN_STOCK,
    "отменен": SupplyPhase.TERMINAL,
    "завершен": SupplyPhase.TERMINAL,
    "завершен успешно": SupplyPhase.TERMINAL,
}

# Фазы, заказы которых учитываются как ожидаемое поступление (уменьшают потребность).
NETTING_PHASES = {SupplyPhase.IN_TRANSIT, SupplyPhase.IN_STOCK}

# Человекочитаемые подписи фаз (для журнала / UI fallback).
PHASE_LABELS = {
    SupplyPhase.NO_GOODS: "Нет товара",
    SupplyPhase.IN_TRANSIT: "Товар в пути",
    SupplyPhase.IN_STOCK: "На складе",
    SupplyPhase.TERMINAL: "Закрыт",
    SupplyPhase.UNKNOWN: "Не определён",
}


def normalize_state(value: Any) -> str:
    """Привести имя состояния 1С к каноническому виду для сравнения."""
    return str(value or "").strip().casefold().replace("ё", "е")


def phase_for_state(value: Any) -> SupplyPhase:
    """
    Вернуть фазу для имени состояния 1С. Пустое имя → UNKNOWN без лога
    (нет состояния — нормальная ситуация для черновиков). Непустое незнакомое
    имя → UNKNOWN с однократным предупреждением в лог.
    """
    norm = normalize_state(value)
    if not norm:
        return SupplyPhase.UNKNOWN
    phase = STATE_TO_PHASE.get(norm)
    if phase is None:
        if norm not in _warned_unknown_states:
            _warned_unknown_states.add(norm)
            logger.warning(
                "Неизвестное состояние заказа поставщику 1С: %r — трактуется как "
                "UNKNOWN и НЕ учитывается в расчёте MRP. Добавьте его в STATE_TO_PHASE.",
                value,
            )
        return SupplyPhase.UNKNOWN
    return phase


def state_counts_in_mrp(value: Any) -> bool:
    """Учитывается ли заказ в этом состоянии как ожидаемое поступление в MRP."""
    return phase_for_state(value) in NETTING_PHASES


def state_is_terminal(value: Any) -> bool:
    """Терминальное ли состояние (заказ закрыт: отменён / завершён)."""
    return phase_for_state(value) is SupplyPhase.TERMINAL


def phase_value(value: Any) -> str:
    """Строковое значение фазы (для сериализации в API/журнал)."""
    return phase_for_state(value).value
