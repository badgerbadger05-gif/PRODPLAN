from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

from ..database import get_db
from ..services.nomenclature_search import search_nomenclature_service

router = APIRouter(prefix="/v1/nomenclature", tags=["nomenclature"])


class SearchRequest(BaseModel):
    q: str
    limit: int = 20
    use_semantic: bool = True
    threshold: float = 0.3


class SearchResponse(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    query: str
    search_type: str  # 'semantic' или 'text'


class GenerateEmbeddingsRequest(BaseModel):
    pass


class GenerateEmbeddingsResponse(BaseModel):
    created: int
    updated: int
    total: int


@router.get("/search")
async def search_nomenclature(
    q: str = Query(..., description="Поисковый запрос"),
    limit: int = Query(20, description="Максимальное количество результатов", ge=1, le=100),
    use_semantic: bool = Query(True, description="Использовать семантический поиск"),
    threshold: float = Query(0.3, description="Порог схожести для семантического поиска", ge=0.0, le=1.0),
    db: Session = Depends(get_db)
):
    """
    Поиск номенклатуры с поддержкой семантического поиска

    - **q**: Поисковый запрос (минимум 2 символа)
    - **limit**: Максимальное количество результатов (1-100)
    - **use_semantic**: Использовать семантический поиск (если доступны эмбеддинги)
    - **threshold**: Минимальная схожесть для семантического поиска (0.0-1.0)
    """
    try:
        results = search_nomenclature_service(
            db=db,
            query=q,
            limit=limit,
            use_semantic=use_semantic,
            threshold=threshold
        )

        # Определяем тип поиска
        search_type = "semantic" if use_semantic and any(
            r.get('similarity', 1.0) < 1.0 for r in results
        ) else "text"

        return SearchResponse(
            items=results,
            total=len(results),
            query=q,
            search_type=search_type
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка поиска: {str(e)}")


@router.post("/generate-embeddings")
async def generate_embeddings(
    request: GenerateEmbeddingsRequest,
    db: Session = Depends(get_db)
):
    """
    Генерация эмбеддингов для всех элементов номенклатуры

    Создает или обновляет векторные представления для всех активных элементов номенклатуры.
    """
    try:
        from ..services.nomenclature_search import NomenclatureSearchService

        service = NomenclatureSearchService(db)
        result = service.generate_embeddings_for_all_items()

        return GenerateEmbeddingsResponse(**result)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка генерации эмбеддингов: {str(e)}")


@router.get("/stats")
async def get_nomenclature_stats(db: Session = Depends(get_db)):
    """
    Получить статистику по номенклатуре и эмбеддингам
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