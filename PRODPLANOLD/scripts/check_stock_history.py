import sqlite3

conn = sqlite3.connect('data/specifications.db')
cursor = conn.execute('PRAGMA table_info(stock_history)')
print('Stock history table structure:')
for row in cursor.fetchall():
    print(f'  {row}')
    
# Проверим несколько записей из таблицы stock_history
cursor = conn.execute('SELECT * FROM stock_history ORDER BY recorded_at DESC LIMIT 5')
print('\nRecent stock history records:')
for row in cursor.fetchall():
    print(f'  {row}')
    
conn.close()