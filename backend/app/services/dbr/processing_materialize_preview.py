"""Safe preview of a 1C toll-processing supplier order.

This module deliberately has no OData client and no write function.  The
payload is based on the currently observed ``Document_ЗаказПоставщику`` shape,
but remains gated until the target 1C base confirms that shape.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...models import (
    DbrFeederSignal,
    DefaultSpecification,
    Item,
    SpecComponent,
    Specification,
)
from ..one_c_export_common import clean_ref1c

ENTITY = "Document_ЗаказПоставщику"
PROCESSING_OPERATION = "ЗаказНаПереработку"
PROCESSING_BUSINESS_OPERATION_KEY = "8d96f6a2-9934-11eb-e39a-fa163e61326a"
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"


class ProcessingPreviewConflict(Exception):
    pass


def _required_ref(value: Any, *, message: str) -> str:
    ref = clean_ref1c(value)
    if not ref:
        raise ProcessingPreviewConflict(message)
    return ref


def _default_spec(db: Session, item_id: int) -> Specification:
    row = (
        db.query(Specification)
        .join(DefaultSpecification, DefaultSpecification.spec_id == Specification.spec_id)
        .filter(DefaultSpecification.item_id == int(item_id))
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    if row is None:
        raise ProcessingPreviewConflict(
            f"item_id={item_id}: не назначена основная спецификация"
        )
    return row


def preview_processing_signal(db: Session, signal_id: int) -> dict[str, Any]:
    """Build a non-writing preview for one processing feeder signal."""
    signal = db.get(DbrFeederSignal, int(signal_id))
    if signal is None:
        raise LookupError("feeder signal not found")
    if signal.position is None or signal.position.supply_type != "processing":
        raise ProcessingPreviewConflict("сигнал не относится к снабжению «Переработка»")
    if signal.status != "Open":
        raise ProcessingPreviewConflict(f"сигнал имеет статус {signal.status}, ожидается Open")
    if signal.is_incomplete:
        raise ProcessingPreviewConflict("сигнал неполный и не может быть подготовлен")

    output = signal.item
    output_ref = _required_ref(
        output.item_ref1c,
        message=f"item_id={output.item_id}: отсутствует item_ref1c покрытой детали",
    )
    supplier_ref = _required_ref(
        output.supplier_ref1c,
        message=f"item_id={output.item_id}: не назначен подрядчик переработки",
    )
    spec = _default_spec(db, int(output.item_id))
    spec_ref = _required_ref(
        spec.spec_ref1c,
        message=f"spec_id={spec.spec_id}: отсутствует spec_ref1c",
    )

    assembly_rows = (
        db.query(SpecComponent, Item)
        .join(Item, Item.item_id == SpecComponent.item_id)
        .filter(
            SpecComponent.spec_id == int(spec.spec_id),
            SpecComponent.component_type == "Сборка",
        )
        .order_by(SpecComponent.component_id.asc())
        .all()
    )
    if len(assembly_rows) != 1:
        raise ProcessingPreviewConflict(
            f"spec_id={spec.spec_id}: ожидается ровно один компонент «Сборка», "
            f"найдено {len(assembly_rows)}"
        )
    component, bare = assembly_rows[0]
    bare_ref = _required_ref(
        bare.item_ref1c,
        message=f"item_id={bare.item_id}: отсутствует item_ref1c голой детали",
    )

    output_qty = float(signal.suggested_qty or 0)
    if output_qty <= 0:
        raise ProcessingPreviewConflict("количество сигнала должно быть больше нуля")
    bare_qty = output_qty * float(component.quantity or 0)
    if bare_qty <= 0:
        raise ProcessingPreviewConflict("количество давальческого компонента должно быть больше нуля")

    payload = {
        "Number": f"DBRP{int(signal.id):07d}",
        "Контрагент_Key": supplier_ref,
        "ВидОперации": PROCESSING_OPERATION,
        "ХозяйственнаяОперация_Key": PROCESSING_BUSINESS_OPERATION_KEY,
        "Комментарий": (
            f"PRODPLAN PREVIEW ONLY; source=dbr/feeder_signal/{int(signal.id)}; "
            "1C shape not live-confirmed"
        ),
        "Запасы": [
            {
                "LineNumber": 1,
                "Номенклатура_Key": output_ref,
                "Характеристика_Key": EMPTY_REF1C,
                "Количество": output_qty,
                "Спецификация_Key": spec_ref,
            }
        ],
        "Материалы": [
            {
                "LineNumber": 1,
                "Номенклатура_Key": bare_ref,
                "Характеристика_Key": EMPTY_REF1C,
                "Количество": bare_qty,
                "Спецификация_Key": clean_ref1c(component.component_spec_ref1c)
                or EMPTY_REF1C,
            }
        ],
    }
    return {
        "dry_run": True,
        "write_capable": False,
        "live_contract_confirmed": False,
        "gate": "blocked_until_1c_contract_confirmation",
        "entity": ENTITY,
        "signal_id": int(signal.id),
        "payload": payload,
    }
