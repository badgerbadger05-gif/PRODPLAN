"""
Скрипт для просмотра данных в таблице items базы данных SQLite.

Этот скрипт подключается к базе данных specifications.db и выводит:
- Список товаров с их кодами, названиями, количеством на складе и статусами
- Общее количество товаров в базе
- Количество товаров по статусам

Использование:
python scripts/view_items.py
"""

import sqlite3
from pathlib import Path

def view_items():
    """Получает и выводит информацию о товарах из таблицы items."""
    db_path = Path("data/specifications.db")
    if not db_path.exists():
        print(f"База данных не найдена по пути: {db_path}")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Получаем все товары
    cursor.execute("""
        SELECT item_id, item_code, item_name, stock_qty, status 
        FROM items 
        ORDER BY item_code
    """)
    items = cursor.fetchall()
    
    print("Товары в базе данных:")
    print(f"{'ID':<5} {'Код':<15} {'Наименование':<50} {'Количество':<12} {'Статус'}")
    print("-" * 100)
    
    for item in items:
        item_id, item_code, item_name, stock_qty, status = item
        # Ограничиваем длину названия для лучшего отображения
        short_name = item_name[:47] + "..." if len(item_name) > 50 else item_name
        print(f"{item_id:<5} {item_code:<15} {short_name:<50} {stock_qty:<12} {status}")
    
    # Получаем общее количество товаров
    cursor.execute("SELECT COUNT(*) FROM items")
    total_items = cursor.fetchone()[0]
    print(f"\nОбщее количество товаров в базе: {total_items}")
    
    # Получаем количество товаров по статусам
    cursor.execute("SELECT status, COUNT(*) FROM items GROUP BY status")
    status_counts = cursor.fetchall()
    print("\nКоличество товаров по статусам:")
    for status, count in status_counts:
        print(f"  {status}: {count}")
    
    conn.close()

if __name__ == "__main__":
    view_items()