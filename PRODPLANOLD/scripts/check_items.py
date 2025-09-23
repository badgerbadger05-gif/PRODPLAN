import sqlite3

conn = sqlite3.connect('data/specifications.db')
cursor = conn.execute('PRAGMA table_info(items)')
print('Items table structure:')
for row in cursor.fetchall():
    print(f'  {row}')
    
# Проверим несколько записей из таблицы items
cursor = conn.execute('SELECT item_code, item_name, stage_id, stock_qty FROM items LIMIT 5')
print('\nSample items:')
for row in cursor.fetchall():
    print(f'  {row}')
    
conn.close()