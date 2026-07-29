"""Поиск номенклатуры.

Раньше здесь жил «семантический» путь: вектор считался как md5 текста,
разложенный на 4 float, и косинусная близость между такими векторами не несёт
никакого смысла. При этом если таблица ``item_embeddings`` была непуста, поиск
возвращал именно эти псевдорезультаты ВМЕСТО рабочего текстового поиска.

Псевдосемантика убрана: поиск всегда текстовый. Таблица ``item_embeddings``
и ORM-модель ``ItemEmbedding`` оставлены под будущее честное решение
(настоящая embedding-модель + векторный индекс).
"""

from typing import List, Dict, Any
from sqlalchemy.orm import Session
from ..models import Item
import logging

logger = logging.getLogger(__name__)


class NomenclatureSearchService:
    """Сервис поиска номенклатуры (текстовый поиск по коду/имени/артикулу)."""

    def __init__(self, db: Session):
        self.db = db

    def search_nomenclature(
        self,
        query: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Поиск номенклатуры текстовым совпадением.

        Args:
            query: Поисковый запрос (минимум 2 значащих символа)
            limit: Максимальное количество результатов

        Returns:
            Список найденных элементов номенклатуры
        """
        if not query or len(query.strip()) < 2:
            return []

        return self._text_search(query.strip(), limit)

    def _text_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """Текстовый поиск по полям номенклатуры"""
        search_term = f"%{query}%"

        results = self.db.query(Item).filter(
            (Item.item_name.ilike(search_term)) |
            (Item.item_code.ilike(search_term)) |
            (Item.item_article.ilike(search_term))
        ).limit(limit).all()

        return [
            {
                'item_id': item.item_id,
                'item_code': item.item_code,
                'item_name': item.item_name,
                'item_article': item.item_article,
                'similarity': 1.0  # Текстовое совпадение: ранжирования нет
            }
            for item in results
        ]


def search_nomenclature_service(
    db: Session,
    query: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Удобная функция для поиска номенклатуры"""
    service = NomenclatureSearchService(db)
    return service.search_nomenclature(query, limit)
