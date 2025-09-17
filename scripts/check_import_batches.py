import sqlite3

conn = sqlite3.connect('data/specifications.db')
cursor = conn.execute('PRAGMA table_info(import_batches)')
print('Import batches table structure:')
for row in cursor.fetchall():
    print(f'  {row}')
    
# Проверим несколько записей из таблицы import_batches
cursor = conn.execute('SELECT * FROM import_batches ORDER BY started_at DESC LIMIT 5')
print('\nRecent import batches:')
for row in cursor.fetchall():
    print(f'  {row}')
    
conn.close()