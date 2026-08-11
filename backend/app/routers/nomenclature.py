from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Literal
from pydantic import BaseModel, ConfigDict

from ..database import get_db
from ..services.nomenclature_search import search_nomenclature_service

router = APIRouter(prefix="/v1/nomenclature", tags=["nomenclature"])


class NomenclatureSearchItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    item_code: str
    item_name: str
    item_article: str | None
    similarity: float


class NomenclatureSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[NomenclatureSearchItemResponse]
    total: int
    query: str
    # Оставлено ради совместимости формы ответа с фронтом; всегда 'text'.
    search_type: Literal["text"]


@router.get("/search", response_model=NomenclatureSearchResponse)
async def search_nomenclature(
    q: str = Query(..., description="Поисковый запрос"),
    limit: int = Query(20, description="Максимальное количество результатов", ge=1, le=100),
    db: Session = Depends(get_db)
) -> NomenclatureSearchResponse:
    """
    Текстовый поиск номенклатуры по наименованию, коду и артикулу.

    - **q**: Поисковый запрос (минимум 2 символа)
    - **limit**: Максимальное количество результатов (1-100)

    «Семантический» режим убран: он строился на md5-псевдовекторах и при
    непустой таблице item_embeddings подменял собой рабочий текстовый поиск.
    """
    try:
        results = search_nomenclature_service(db=db, query=q, limit=limit)

        return NomenclatureSearchResponse(
            items=results,
            total=len(results),
            query=q,
            search_type="text",
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка поиска: {str(e)}")


@router.get("/stats")
async def get_nomenclature_stats(db: Session = Depends(get_db)):
    """
    Получить статистику по номенклатуре и эмбеддингам.

    Заполнение item_embeddings убрано вместе с псевдосемантическим поиском:
    сейчас items_with_embeddings отражает только исторические строки.
    """
    try:
        from ..models import Item, ItemEmbedding

        total_items = db.query(Item).filter(Item.status == 'active').count()
        items_with_embeddings = db.query(ItemEmbedding).count()

        return {
            "total_items": total_items,
            "items_with_embeddings": items_with_embeddings,
            "embeddings_coverage": items_with_embeddings / total_items if total_items > 0 else 0
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка получения статистики: {str(e)}")
