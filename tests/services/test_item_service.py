from decimal import Decimal

from app import models, schemas
from app.services.item_service import update_item


def test_put_item_does_not_replace_ledger_owned_stock(db_session):
    item = models.Item(
        item_code="ITEM-PUT-STOCK",
        item_name="Before",
        stock_qty=Decimal("17.5"),
    )
    db_session.add(item)
    db_session.commit()

    updated = update_item(
        db_session,
        int(item.item_id),
        schemas.ItemUpdate(
            item_code=item.item_code,
            item_name="After",
            stock_qty=0,
        ),
    )

    assert updated.item_name == "After"
    assert Decimal(str(updated.stock_qty)) == Decimal("17.5")


def test_put_item_omitted_stock_default_is_not_persisted(db_session):
    item = models.Item(
        item_code="ITEM-PUT-OMITTED-STOCK",
        item_name="Before",
        stock_qty=Decimal("9"),
    )
    db_session.add(item)
    db_session.commit()

    updated = update_item(
        db_session,
        int(item.item_id),
        schemas.ItemUpdate(
            item_code=item.item_code,
            item_name="After",
        ),
    )

    assert updated.item_name == "After"
    assert Decimal(str(updated.stock_qty)) == Decimal("9")
