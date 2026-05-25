import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'backend')))

from app.services.odata_client import OData1CClient

BASE_URL = os.getenv('ODATA_BASE_URL', 'http://mtzw7/unf/odata/standard.odata')
USERNAME = os.getenv('ODATA_USERNAME')
PASSWORD = os.getenv('ODATA_PASSWORD')

if not USERNAME or not PASSWORD:
    raise SystemExit('Set ODATA_USERNAME and ODATA_PASSWORD before running this diagnostic.')

client = OData1CClient(BASE_URL, USERNAME, PASSWORD)

print('Loading production orders...')
data = client.get_all(
    'Document_ЗаказНаПроизводство',
    select_fields=['Ref_Key', 'Number', 'СостояниеЗаказа_Key', 'DeletionMark'],
    top=500,
)

states = {}
deleted_count = 0
for rec in data:
    deletion_mark = rec.get('DeletionMark', False)
    if deletion_mark is True or deletion_mark == 'true':
        deleted_count += 1
        continue
    key = str(rec.get('СостояниеЗаказа_Key', '') or '').strip()
    if key and key not in states:
        states[key] = rec.get('Number', '')

print(f'\nTotal orders: {len(data)}')
print(f'Deleted: {deleted_count}')
print('\nStates found in active orders:')
for key, example in states.items():
    print(f'  {key} (example: {example})')
