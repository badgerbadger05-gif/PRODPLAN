import sys
sys.path.insert(0, 'backend')
from app.services.odata_client import OData1CClient

client = OData1CClient('http://mtzw7/unf/odata/standard.odata', 'odata.user', 'Gw6dwAEKmm$o')

# Получаем первые 500 заказов с состояниями
print("Загрузка заказов...")
data = client.get_all('Document_ЗаказНаПроизводство', select_fields=['Ref_Key', 'Number', 'СостояниеЗаказа_Key', 'DeletionMark'], top=500)

states = {}
deleted_count = 0
for rec in data:
    dm = rec.get('DeletionMark', False)
    if dm is True or dm == "true":
        deleted_count += 1
        continue
    key = str(rec.get('СостояниеЗаказа_Key', '') or '').strip()
    if key and key not in states:
        states[key] = rec.get('Number', '')

print(f'\nВсего заказов: {len(data)}')
print(f'Удалённых: {deleted_count}')
print(f'\nНайдены состояния (активные заказы):')
for k, v in states.items():
    print(f'  {k} (пример: {v})')
