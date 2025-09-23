import sqlite3

conn = sqlite3.connect('data/specifications.db')
cursor = conn.execute('SELECT stage_id, stage_name FROM production_stages')
stages = cursor.fetchall()
print('Production stages:')
for stage in stages:
    print(f'  {stage}')
    
# Проверим несколько товаров с NULL stage_id
cursor = conn.execute('SELECT item_code, item_name, stage_id FROM items WHERE stage_id IS NULL LIMIT 5')
print('\nItems with NULL stage_id:')
for row in cursor.fetchall():
    print(f'  {row}')
    
conn.close()