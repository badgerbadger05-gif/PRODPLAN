import json
import numpy as np
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from ..models import Item, ItemEmbedding
import logging

logger = logging.getLogger(__name__)


class NomenclatureSearchService:
    """Сервис для семантического поиска номенклатуры"""

    def __init__(self, db: Session):
        self.db = db
        self.embedding_model = None

    def search_nomenclature(
        self,
        query: str,
        limit: int = 20,
        use_semantic: bool = True,
        threshold: float = 0.3
    ) -> List[Dict[str, Any]]:
        """
        Поиск номенклатуры с использованием семантического поиска или текстового поиска

        Args:
            query: Поисковый запрос
            limit: Максимальное количество результатов
            use_semantic: Использовать семантический поиск
            threshold: Порог схожести для семантического поиска

        Returns:
            Список найденных элементов номенклатуры
        """
        if not query or len(query.strip()) < 2:
            return []

        query = query.strip()

        # Попытка семантического поиска
        if use_semantic:
            try:
                semantic_results = self._semantic_search(query, limit, threshold)
                if semantic_results:
                    return semantic_results
            except Exception as e:
                logger.warning(f"Семантический поиск не удался: {e}")

        # Fallback на текстовый поиск
        return self._text_search(query, limit)

    def _semantic_search(
        self,
        query: str,
        limit: int,
        threshold: float
    ) -> List[Dict[str, Any]]:
        """Семантический поиск по эмбеддингам"""
        # Проверяем наличие эмбеддингов в БД
        embedding_count = self.db.query(ItemEmbedding).count()
        if embedding_count == 0:
            logger.info("Эмбеддинги не найдены в БД, пропускаем семантический поиск")
            return []

        # Генерируем эмбеддинг для запроса
        try:
            query_embedding = self._generate_embedding(query)
            if not query_embedding:
                return []

            # Ищем похожие эмбеддинги через косинусную схожесть
            # Получаем все эмбеддинги и вычисляем схожесть в Python
            embeddings_result = self.db.execute(text("""
                SELECT
                    i.item_id,
                    i.item_code,
                    i.item_name,
                    i.item_article,
                    ie.embedding_vector
                FROM items i
                JOIN item_embeddings ie ON i.item_id = ie.item_id
            """)).fetchall()
            
            results = []
            for row in embeddings_result:
                try:
                    item_embedding = json.loads(row.embedding_vector)
                    similarity = self._calculate_similarity(query_embedding, item_embedding)
                    if similarity > threshold:
                        results.append({
                            'item_id': row.item_id,
                            'item_code': row.item_code,
                            'item_name': row.item_name,
                            'item_article': row.item_article,
                            'similarity': similarity
                        })
                except Exception as e:
                    logger.warning(f"Ошибка обработки эмбеддинга для item_id {row.item_id}: {e}")
                    continue
            
            # Сортируем по убыванию схожести и ограничиваем количество
            results.sort(key=lambda x: x['similarity'], reverse=True)
            results = results[:limit]

            return results

        except Exception as e:
            logger.error(f"Ошибка при семантическом поиске: {e}")
            return []

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
                'similarity': 1.0  # Для текстового поиска ставим максимальную схожесть
            }
            for item in results
        ]

    def _generate_embedding(self, text: str) -> Optional[List[float]]:
        """Генерация эмбеддинга для текста"""
        try:
            # Простая реализация - в будущем можно добавить интеграцию с моделями
            # Пока используем заглушку для демонстрации
            import hashlib
            import struct

            # Генерируем детерминированный вектор на основе хеша текста
            # Это временное решение для демонстрации
            hash_obj = hashlib.md5(text.encode('utf-8'))
            hash_bytes = hash_obj.digest()

            # Преобразуем 16 байт хеша в 4 float32 значения
            vector = []
            for i in range(0, len(hash_bytes), 4):
                chunk = hash_bytes[i:i+4]
                if len(chunk) == 4:
                    float_val = struct.unpack('f', chunk)[0]
                    vector.append(float_val)

            # Нормализуем вектор
            vector = np.array(vector)
            norm = np.linalg.norm(vector)
            if norm > 0:
                vector = vector / norm

            return vector.tolist()

        except Exception as e:
            logger.error(f"Ошибка при генерации эмбеддинга: {e}")
            return None

    def _calculate_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Вычисляет косинусную схожесть между двумя векторами"""
        try:
            # Приводим к numpy массивам
            v1 = np.array(vec1)
            v2 = np.array(vec2)
            
            # Проверяем размерности
            if len(v1) != len(v2):
                return 0.0
            
            # Вычисляем косинусную схожесть
            dot_product = np.dot(v1, v2)
            norm1 = np.linalg.norm(v1)
            norm2 = np.linalg.norm(v2)
            
            if norm1 == 0 or norm2 == 0:
                return 0.0
            
            similarity = dot_product / (norm1 * norm2)
            return float(np.clip(similarity, 0.0, 1.0))
        except Exception as e:
            logger.error(f"Ошибка вычисления схожести: {e}")
            return 0.0

    def generate_embeddings_for_all_items(self) -> Dict[str, int]:
        """Генерация эмбеддингов для всех элементов номенклатуры"""
        try:
            items = self.db.query(Item).filter(Item.status == 'active').all()

            created_count = 0
            updated_count = 0

            for item in items:
                # Создаем текст для эмбеддинга
                text_parts = []
                if item.item_name:
                    text_parts.append(item.item_name)
                if item.item_article:
                    text_parts.append(item.item_article)
                if item.item_code:
                    text_parts.append(item.item_code)

                text = ' '.join(text_parts)

                if text.strip():
                    embedding = self._generate_embedding(text)
                    if embedding:
                        # Проверяем, существует ли уже эмбеддинг
                        existing = self.db.query(ItemEmbedding).filter(
                            ItemEmbedding.item_id == item.item_id
                        ).first()

                        if existing:
                            existing.embedding_vector = json.dumps(embedding)
                            existing.updated_at = func.now()
                            updated_count += 1
                        else:
                            new_embedding = ItemEmbedding(
                                item_id=item.item_id,
                                embedding_vector=json.dumps(embedding)
                            )
                            self.db.add(new_embedding)
                            created_count += 1

            self.db.commit()

            return {
                'created': created_count,
                'updated': updated_count,
                'total': created_count + updated_count
            }

        except Exception as e:
            self.db.rollback()
            logger.error(f"Ошибка при генерации эмбеддингов: {e}")
            raise


def search_nomenclature_service(
    db: Session,
    query: str,
    limit: int = 20,
    use_semantic: bool = True,
    threshold: float = 0.3
) -> List[Dict[str, Any]]:
    """Удобная функция для поиска номенклатуры"""
    service = NomenclatureSearchService(db)
    return service.search_nomenclature(query, limit, use_semantic, threshold)