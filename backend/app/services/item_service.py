from sqlalchemy.orm import Session
from ..models import Item
from ..schemas import ItemCreate, ItemPatch, ItemUpdate


def get_items(db: Session, skip: int = 0, limit: int = 100):
    items = db.query(Item).offset(skip).limit(limit).all()
    total = db.query(Item).count()
    return {
        "rows": items,
        "total": total,
        "limit": limit,
        "offset": skip
    }


def get_item(db: Session, item_id: int):
    return db.query(Item).filter(Item.item_id == item_id).first()


def create_item(db: Session, item: ItemCreate):
    db_item = Item(**item.model_dump())
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item


def update_item(db: Session, item_id: int, item: ItemUpdate):
    db_item = db.query(Item).filter(Item.item_id == item_id).first()
    if db_item:
        # Physical stock is sync/Ledger-owned.  A full-record editor must not
        # be able to overwrite it with either ItemBase's default zero or a
        # stale value echoed from an earlier GET.
        changes = item.model_dump(exclude_unset=True)
        changes.pop("stock_qty", None)
        for key, value in changes.items():
            setattr(db_item, key, value)
        db.commit()
        db.refresh(db_item)
    return db_item


def update_item_partial(db: Session, item_id: int, patch: ItemPatch):
    """Write only the attributes the caller actually sent.

    A partial update touches nothing else and never `stock_qty`: the schema
    rejects it, and the pop below keeps that true even if a future field set
    reintroduces it.
    """
    db_item = db.query(Item).filter(Item.item_id == item_id).first()
    if db_item is None:
        return None
    changes = patch.model_dump(exclude_unset=True)
    changes.pop("stock_qty", None)
    if not changes:
        return db_item
    for key, value in changes.items():
        setattr(db_item, key, value)
    db.commit()
    db.refresh(db_item)
    return db_item


def delete_item(db: Session, item_id: int):
    db_item = db.query(Item).filter(Item.item_id == item_id).first()
    if db_item:
        db.delete(db_item)
        db.commit()
    return db_item
