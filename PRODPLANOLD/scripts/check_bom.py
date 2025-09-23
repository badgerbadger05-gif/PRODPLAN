import sqlite3

conn = sqlite3.connect('data/specifications.db')
cursor = conn.execute('PRAGMA table_info(bom)')
print('BOM table structure:')
for row in cursor.fetchall():
    print(f'  {row}')
    
# Проверим несколько записей из таблицы bom
cursor = conn.execute('SELECT * FROM bom LIMIT 5')
print('\nSample BOM records:')
for row in cursor.fetchall():
    print(f'  {row}')
    
# Проверим связи между изделиями
cursor = conn.execute('''
    SELECT 
        p.item_code as parent_code,
        p.item_name as parent_name,
        c.item_code as child_code,
        c.item_name as child_name,
        b.quantity
    FROM bom b
    JOIN items p ON p.item_id = b.parent_item_id
    JOIN items c ON c.item_id = b.child_item_id
    LIMIT 10
''')
print('\nSample BOM relationships:')
for row in cursor.fetchall():
    print(f'  Parent: {row[0]} ({row[1]}) -> Child: {row[2]} ({row[3]}) Qty: {row[4]}')

conn.close()