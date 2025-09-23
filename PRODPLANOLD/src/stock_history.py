"""
Модуль для работы с историей остатков.

Этот модуль предоставляет функции для:
- Хранения истории остатков с течением времени
- Анализа динамики изменения остатков
- Расчета потребностей на основе исторических данных
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List, Tuple, Union

from .database import get_connection


def init_stock_history_table(db_path: Optional[Path] = None) -> None:
    """
    Инициализация таблицы истории остатков.
    
    Создает таблицу stock_history для хранения истории остатков
    с течением времени.
    """
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_code TEXT NOT NULL,
                stock_qty REAL NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY(item_code) REFERENCES items(item_code) ON DELETE CASCADE
            )
        """)
        
        # Создаем индекс для ускорения запросов
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stock_history_item_date 
            ON stock_history(item_code, recorded_at)
        """)


def save_stock_snapshot(db_path: Optional[Path] = None) -> None:
    """
    Сохраняет текущие остатки как снимок в историю.
    
    Копирует текущие значения остатков из таблицы items
    в таблицу stock_history.
    """
    with get_connection(db_path) as conn:
        # Вставляем текущие остатки в историю
        conn.execute("""
            INSERT INTO stock_history (item_code, stock_qty, recorded_at)
            SELECT item_code, stock_qty, datetime('now')
            FROM items
            WHERE stock_qty IS NOT NULL
        """)


def cleanup_old_stock_history(days_to_keep: int = 30, db_path: Optional[Path] = None) -> None:
    """
    Удаляет старые записи из истории остатков.
    
    Args:
        days_to_keep: Количество дней истории, которые нужно сохранить
        db_path: Путь к базе данных
    """
    with get_connection(db_path) as conn:
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        conn.execute("""
            DELETE FROM stock_history 
            WHERE recorded_at < ?
        """, (cutoff_date.isoformat(),))


def get_stock_history(item_code: str, days: int = 30, db_path: Optional[Path] = None) -> List[Tuple[str, float]]:
    """
    Получает историю остатков для конкретного товара.
    
    Args:
        item_code: Код товара
        days: Количество дней истории
        db_path: Путь к базе данных
        
    Returns:
        Список кортежей (дата, количество)
    """
    with get_connection(db_path) as conn:
        cutoff_date = datetime.now() - timedelta(days=days)
        cursor = conn.execute("""
            SELECT recorded_at, stock_qty
            FROM stock_history
            WHERE item_code = ? AND recorded_at >= ?
            ORDER BY recorded_at ASC
        """, (item_code, cutoff_date.isoformat()))
        
        return cursor.fetchall()


def get_stock_trend(item_code: str, days: int = 30, db_path: Optional[Path] = None) -> Dict[str, float]:
    """
    Анализирует тенденцию изменения остатков.
    
    Args:
        item_code: Код товара
        days: Количество дней для анализа
        db_path: Путь к базе данных
        
    Returns:
        Словарь с анализом тенденции:
        {
            'avg_daily_change': среднее_daily изменение,
            'trend': 'increasing' | 'decreasing' | 'stable',
            'consumption_rate': средняя скорость потребления (если отрицательная)
        }
    """
    history = get_stock_history(item_code, days, db_path)
    
    if len(history) < 2:
        return {
            'avg_daily_change': 0.0,
            'trend': 'stable',
            'consumption_rate': 0.0
        }
    
    # Рассчитываем среднее_daily изменение
    total_change = 0.0
    for i in range(1, len(history)):
        total_change += history[i][1] - history[i-1][1]
    
    avg_daily_change = total_change / (len(history) - 1)
    
    # Определяем тенденцию
    if avg_daily_change > 0.1:
        trend = 'increasing'
    elif avg_daily_change < -0.1:
        trend = 'decreasing'
    else:
        trend = 'stable'
    
    # Рассчитываем скорость потребления (если остатки уменьшаются)
    consumption_rate = 0.0
    if avg_daily_change < 0:
        consumption_rate = abs(avg_daily_change)
    
    return {
        'avg_daily_change': avg_daily_change,
        'trend': trend,
        'consumption_rate': consumption_rate
    }  # type: ignore


def predict_stock_depletion(item_code: str, db_path: Optional[Path] = None) -> Optional[int]:
    """
    Прогнозирует, через сколько дней закончатся остатки.
    
    Args:
        item_code: Код товара
        db_path: Путь к базе данных
        
    Returns:
        Количество дней до исчерпания остатков или None, если прогноз невозможен
    """
    trend = get_stock_trend(item_code, 30, db_path)
    
    if trend['consumption_rate'] <= 0:
        return None  # Остатки не уменьшаются, прогноз невозможен
    
    with get_connection(db_path) as conn:
        cursor = conn.execute("""
            SELECT stock_qty FROM items WHERE item_code = ?
        """, (item_code,))
        row = cursor.fetchone()
        
        if not row:
            return None
            
        current_stock = row[0]
        
        if current_stock <= 0:
            return 0  # Остатки уже закончились
            
        # Рассчитываем количество дней до исчерпания
        days_until_depletion = int(current_stock / trend['consumption_rate'])
        return days_until_depletion


def get_items_needing_restock(threshold_days: int = 7, db_path: Optional[Path] = None) -> List[Tuple[str, int]]:
    """
    Получает список товаров, которые могут закончиться в ближайшее время.
    
    Args:
        threshold_days: Пороговое значение дней для предупреждения
        db_path: Путь к базе данных
        
    Returns:
        Список кортежей (код товара, дней до исчерпания)
    """
    with get_connection(db_path) as conn:
        cursor = conn.execute("""
            SELECT item_code FROM items WHERE stock_qty > 0
        """)
        items = cursor.fetchall()
    
    items_needing_restock = []
    
    for item in items:
        item_code = item[0]
        days_until_depletion = predict_stock_depletion(item_code, db_path)
        
        if days_until_depletion is not None and days_until_depletion <= threshold_days:
            items_needing_restock.append((item_code, days_until_depletion))
    
    # Сортируем по срочности (меньше дней - выше приоритет)
    items_needing_restock.sort(key=lambda x: x[1])
    
    return items_needing_restock


# Обновленная функция синхронизации остатков с сохранением истории
def sync_stock_with_history(
    stock_path: Path | str | None = None,
    db_path: Path | str | None = None,
    dry_run: bool = False,
):
    """
    Синхронизация остатков с сохранением истории.
    
    Args:
        stock_path: Путь к файлу или каталогу с остатками
        db_path: Путь к базе данных
        dry_run: Режим "пробного" запуска без изменений
    """
    from .stock_sync import sync_stock
    
    # Выполняем обычную синхронизацию остатков
    stats = sync_stock(stock_path, db_path, dry_run)
    
    # Если не в режиме пробного запуска, сохраняем снимок
    if not dry_run:
        # Инициализируем таблицу истории (если еще не создана)
        init_stock_history_table(Path(db_path) if db_path else None)
        
        # Сохраняем текущие остатки как снимок
        save_stock_snapshot(Path(db_path) if db_path else None)
        
        # Удаляем старые записи (оставляем только 30 дней)
        cleanup_old_stock_history(30, Path(db_path) if db_path else None)
    
    return stats