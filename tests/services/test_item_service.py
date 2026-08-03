from app import models, schemas
from app.services.item_service import update_item


def test_put_item_updates_master_data_without_physical_field(db_session):
    item = models.Item(item_code="ITEM-PUT", item_name="Before")
    db_session.add(item)
    db_session.commit()

    updated = update_item(
        db_session,
        int(item.item_id),
        schemas.ItemUpdate(item_code=item.item_code, item_name="After"),
    )

    assert updated.item_name == "After"
    assert not hasattr(updated, "stock_qty")
