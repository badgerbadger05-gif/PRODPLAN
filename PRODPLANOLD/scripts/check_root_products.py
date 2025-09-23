import sqlite3

conn = sqlite3.connect('data/specifications.db')
cursor = conn.execute('SELECT * FROM root_products')
print('Root products:')
for row in cursor.fetchall():
    print(f'  {row}')
    
# Получим информацию о корневых изделиях из таблицы items
cursor = conn.execute('''
    SELECT i.item_code, i.item_name 
    FROM items i 
    JOIN root_products rp ON rp.item_id = i.item_id
''')
print('\nRoot products details:')
for row in cursor.fetchall():
    print(f'  {row}')
    
conn.close()