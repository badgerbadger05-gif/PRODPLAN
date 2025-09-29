from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..schemas import Item, ItemCreate, ItemUpdate
from ..services.item_service import get_items, get_item, create_item, update_item, delete_item

router = APIRouter(prefix="/v1/items", tags=["items"])


@router.get("/", response_model=List[Item])
def read_items(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    items = get_items(db, skip=skip, limit=limit)
    return items


@router.post("/", response_model=Item)
def create_item_endpoint(item: ItemCreate, db: Session = Depends(get_db)):
    return create_item(db, item)


@router.get("/{item_id}", response_model=Item)
def read_item(item_id: int, db: Session = Depends(get_db)):
    db_item = get_item(db, item_id=item_id)
    if db_item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return db_item


@router.put("/{item_id}", response_model=Item)
def update_item_endpoint(item_id: int, item: ItemUpdate, db: Session = Depends(get_db)):
    db_item = update_item(db, item_id=item_id, item=item)
    if db_item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return db_item


@router.delete("/{item_id}", response_model=Item)
def delete_item_endpoint(item_id: int, db: Session = Depends(get_db)):
    db_item = delete_item(db, item_id=item_id)
    if db_item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return db_item