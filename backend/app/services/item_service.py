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
        changes = item.model_dump(exclude_unset=True)
        for key, value in changes.items():
            setattr(db_item, key, value)
        db.commit()
        db.refresh(db_item)
    return db_item


def update_item_partial(db: Session, item_id: int, patch: ItemPatch):
    """Write only the master-data attributes the caller actually sent."""
    db_item = db.query(Item).filter(Item.item_id == item_id).first()
    if db_item is None:
        return None
    changes = patch.model_dump(exclude_unset=True)
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
