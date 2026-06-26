"""
Скрипт для исследования заказа ЗСНФ-000943 в 1С через OData.
Последовательно выполняет запросы и выводит результаты.
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import base64
import os

BASE_URL = "http://mtzw7/unf/odata/standard.odata/"
# Учётные данные берём только из переменных окружения (без захардкоженных секретов)
USERNAME = os.getenv("ODATA_USERNAME")
PASSWORD = os.getenv("ODATA_PASSWORD")

def make_request(endpoint, params=None):
    """Выполняет GET запрос к 1С OData."""
    # Кодируем endpoint для поддержки кириллицы
    endpoint_encoded = urllib.parse.quote(endpoint, safe="/")
    url = f"{BASE_URL}{endpoint_encoded}"
    if params:
        # Кодируем параметры с поддержкой кириллицы
        query_parts = []
        for key, value in params.items():
            key_encoded = urllib.parse.quote(str(key), safe='')
            # value кодируем полностью, включая пробелы
            value_encoded = urllib.parse.quote(str(value), safe="$,()*")
            query_parts.append(f"{key_encoded}={value_encoded}")
        query_string = "&".join(query_parts)
        url = f"{url}?{query_string}"
    
    # Для отладки печатаем URL в безопасном виде (без пробелов)
    print(f"\n{'='*80}")
    print(f"URL: {url.replace(' ', '%20')}")
    print('='*80)
    
    # Заменяем пробелы в URL на %20
    url = url.replace(' ', '%20')
    
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/json;odata.metadata=minimal")
    
    # Добавляем аутентификацию
    if USERNAME and PASSWORD:
        credentials = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        request.add_header("Authorization", f"Basic {credentials}")
    
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
            result = json.loads(data.decode("utf-8"))
            return result
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}")
        try:
            err_data = e.read().decode("utf-8", errors="replace")
            print(f"Details: {err_data[:500]}")
        except:
            pass
        return None
    except UnicodeEncodeError as e:
        print(f"Unicode Encode Error: {e}")
        print(f"URL: {url}")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None


def print_result(title, result, max_depth=2, max_items=10):
    """Красиво выводит результат."""
    print(f"\n{'#'*80}")
    print(f"# {title}")
    print('#'*80)
    
    if result is None:
        print("❌ Результат: None")
        return
    
    if isinstance(result, dict):
        if "value" in result:
            items = result["value"]
            print(f"✅ Найдено записей: {len(items)}")
            if items:
                print(f"\n📋 Первые {min(max_items, len(items))} записей:")
                for i, item in enumerate(items[:max_items]):
                    print(f"\n--- Запись {i+1} ---")
                    print(json.dumps(item, ensure_ascii=False, indent=2))
        else:
            print("✅ Результат (dict):")
            print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])
    elif isinstance(result, list):
        print(f"✅ Найдено записей: {len(result)}")
        for i, item in enumerate(result[:max_items]):
            print(f"\n--- Запись {i+1} ---")
            print(json.dumps(item, ensure_ascii=False, indent=2))
    else:
        print(f"✅ Результат: {result}")


def main():
    print("="*80)
    print("ИССЛЕДОВАНИЕ ЗАКАЗА ЗСНФ-000943")
    print("="*80)
    
    # =====================================================================
    # Шаг 1: Найти заказ по номеру (базовый запрос)
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 1: Поиск заказа по номеру ЗСНФ-000943")
    print("="*80)
    
    result = make_request(
        "Document_ЗаказНаПроизводство",
        {
            "$format": "json",
            "$select": "Ref_Key,Number,Date,Posted,DeletionMark,СостояниеЗаказа_Key",
            "$filter": "Number eq 'ЗСНФ-000943'"
        }
    )
    print_result("Шаг 1: Заказ по номеру", result)
    
    order_ref_key = None
    order_state_key = None
    if result and isinstance(result, dict) and "value" in result:
        items = result["value"]
        if items:
            order_ref_key = items[0].get("Ref_Key")
            order_state_key = items[0].get("СостояниеЗаказа_Key")
            print(f"\n🔑 Ref_Key заказа: {order_ref_key}")
            print(f"🔑 Состояние заказа (Key): {order_state_key}")
    
    # =====================================================================
    # Шаг 2: Получить состояние заказа с expand
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 2: Состояние заказа с expand навигацией")
    print("="*80)
    
    result = make_request(
        "Document_ЗаказНаПроизводство",
        {
            "$format": "json",
            "$select": "Ref_Key,Number,Date,Posted,СостояниеЗаказа_Key",
            "$expand": "СостояниеЗаказа",
            "$filter": "Number eq 'ЗСНФ-000943'"
        }
    )
    print_result("Шаг 2: Состояние заказа (expand)", result)
    
    # =====================================================================
    # Шаг 3: Получить табличную часть "Продукция"
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 3: Табличная часть 'Продукция' по заказу")
    print("="*80)
    
    if order_ref_key:
        result = make_request(
            "Document_ЗаказНаПроизводство_Продукция",
            {
                "$format": "json",
                # Этап_Key отсутствует в метаданных 1С для этой сущности!
                # Используем только доступные поля (подтверждено исследованием ЗСНФ-000943):
                "$select": "Ref_Key,LineNumber,Номенклатура_Key,Количество,Характеристика_Key,Спецификация_Key,ПодразделениеЗавершающегоЭтапа_Key",
                "$filter": f"Ref_Key eq guid'{order_ref_key}'"
            }
        )
        print_result("Шаг 3: Продукция заказа", result)
    else:
        print("❌ Пропущено: нет Ref_Key заказа")
    
    # =====================================================================
    # Шаг 4: Найти документы "Сборка запасов"
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 4: Документы 'Сборка запасов', связанные с заказом")
    print("="*80)
    
    if order_ref_key:
        result = make_request(
            "Document_СборкаЗапасов",
            {
                "$format": "json",
                "$select": "Ref_Key,Number,Date,Posted,DeletionMark,ЗаказНаПроизводство_Key",
                "$filter": f"ЗаказНаПроизводство_Key eq guid'{order_ref_key}'"
            }
        )
        print_result("Шаг 4: Сборка запасов", result)
        
        assembly_ref_keys = []
        if result and isinstance(result, dict) and "value" in result:
            assembly_ref_keys = [item.get("Ref_Key") for item in result["value"] if item.get("Ref_Key")]
            print(f"\n🔑 Найдено сборок: {len(assembly_ref_keys)}")
            for i, key in enumerate(assembly_ref_keys[:5]):
                print(f"  {i+1}. {key}")
    else:
        print("❌ Пропущено: нет Ref_Key заказа")
        assembly_ref_keys = []
    
    # =====================================================================
    # Шаг 5: Продукция по сборкам запасов
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 5: Продукция из документов 'Сборка запасов'")
    print("="*80)
    
    if assembly_ref_keys:
        # Загружаем все строки продукции одним запросом
        or_filter = " or ".join([f"Ref_Key eq guid'{k}'" for k in assembly_ref_keys[:50]])
        result = make_request(
            "Document_СборкаЗапасов_Продукция",
            {
                "$format": "json",
                "$select": "Ref_Key,LineNumber,Номенклатура_Key,Характеристика_Key,Количество,Спецификация_Key",
                "$filter": f"({or_filter})"
            }
        )
        print_result("Шаг 5: Продукция сборок", result)
    elif order_ref_key:
        # Альтернатива: через expand
        print("\nАльтернативный запрос через expand:")
        result = make_request(
            "Document_СборкаЗапасов",
            {
                "$format": "json",
                "$select": "Ref_Key,Number,Date,ЗаказНаПроизводство_Key",
                "$expand": "Продукция",
                "$filter": f"ЗаказНаПроизводство_Key eq guid'{order_ref_key}'"
            }
        )
        print_result("Шаг 5 (альт.): Сборка с Продукцией (expand)", result)
    else:
        print("❌ Пропущено: нет данных для запроса")
    
    # =====================================================================
    # Шаг 6: Проверка регистра "Выпуск продукции"
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 6: Регистр 'Выпуск продукции' (альтернативный источник)")
    print("="*80)
    
    result = make_request(
        "AccumulationRegister_ВыпускПродукции.RecordType",
        {
            "$format": "json",
            "$top": "50",
            "$orderby": "Period desc"
        }
    )
    print_result("Шаг 6: Регистр выпуска (последние 50 записей)", result)
    
    # =====================================================================
    # Шаг 7: Проверка всех доступных полей в заказе
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 7: Все доступные поля заказа (без $select)")
    print("="*80)
    
    if order_ref_key:
        result = make_request(
            "Document_ЗаказНаПроизводство",
            {
                "$format": "json",
                "$filter": f"Ref_Key eq guid'{order_ref_key}'"
            }
        )
        print_result("Шаг 7: Все поля заказа", result, max_items=1)
    else:
        print("❌ Пропущено: нет Ref_Key заказа")
    
    # =====================================================================
    # Шаг 8: Проверка всех доступных полей в продукции заказа
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 8: Все доступные поля в продукции заказа")
    print("="*80)
    
    if order_ref_key:
        result = make_request(
            "Document_ЗаказНаПроизводство_Продукция",
            {
                "$format": "json",
                "$filter": f"Ref_Key eq guid'{order_ref_key}'"
            }
        )
        print_result("Шаг 8: Все поля продукции", result, max_items=3)
    else:
        print("❌ Пропущено: нет Ref_Key заказа")
    
    # =====================================================================
    # Шаг 9: Текущий фильтр из production_order_sync.py (навигация)
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 9: Текущий фильтр из production_order_sync.py (навигация)")
    print("="*80)
    
    result = make_request(
        "Document_ЗаказНаПроизводство",
        {
            "$format": "json",
            "$select": "Ref_Key,Number,Date,Posted,DeletionMark,СостояниеЗаказа_Key",
            "$filter": "DeletionMark eq false and Posted eq true",
            "$orderby": "Number",
            "$top": "100"
        }
    )
    print_result("Шаг 9: Текущий фильтр (навигация)", result)
    print("\n📋 Номера заказов из Шага 9:")
    if result and isinstance(result, dict) and "value" in result:
        for item in result["value"][:20]:
            print(f"  - {item.get('Number')} (Состояние: {item.get('СостояниеЗаказа_Key', 'N/A')[:20]}...)")
        if len(result["value"]) > 20:
            print(f"  ... и ещё {len(result['value']) - 20} заказов")
    else:
        print("  ❌ Заказы не получены")
    
    # =====================================================================
    # Шаг 10: Текущий фильтр без исключения завершённых
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 10: Текущий фильтр без исключения завершённых")
    print("="*80)
    
    result = make_request(
        "Document_ЗаказНаПроизводство",
        {
            "$format": "json",
            "$select": "Ref_Key,Number,Date,Posted,DeletionMark,СостояниеЗаказа_Key",
            "$filter": "DeletionMark eq false and Posted eq true",
            "$orderby": "Number",
            "$top": "100"
        }
    )
    print_result("Шаг 10: Текущий фильтр", result)
    print("\n📋 Номера заказов из Шага 10 (первые 30):")
    if result and isinstance(result, dict) and "value" in result:
        for item in result["value"][:30]:
            print(f"  - {item.get('Number')} (Состояние: {item.get('СостояниеЗаказа_Key', 'N/A')[:20]}...)")
        if len(result['value']) > 30:
            print(f"  ... и ещё {len(result['value']) - 30} заказов (всего: {len(result['value'])})")
    else:
        print("  ❌ Заказы не получены")
    
    # =====================================================================
    # Шаг 11: Проверка ЗСНФ-000943 с текущим фильтром
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 11: Проверка ЗСНФ-000943 с текущим фильтром")
    print("="*80)
    
    result = make_request(
        "Document_ЗаказНаПроизводство",
        {
            "$format": "json",
            "$select": "Ref_Key,Number,Date,Posted,DeletionMark,СостояниеЗаказа_Key",
            "$filter": "Number eq 'ЗСНФ-000943' and DeletionMark eq false and Posted eq true"
        }
    )
    print_result("Шаг 11: ЗСНФ-000943 с текущим фильтром", result)
    
    # =====================================================================
    # ИТОГИ
    # =====================================================================
    print("\n\n" + "="*80)
    print("ИТОГИ ИССЛЕДОВАНИЯ")
    print("="*80)
    print(f"""
📋 Заказ ЗСНФ-000943:
   - Ref_Key: {order_ref_key or 'не найден'}
   - Состояние (Key): {order_state_key or 'не получено'}

📦 Продукция:
   - Проверьте Шаг 3 и Шаг 8 для получения структуры заказа

📦 Выпуск (Сборка запасов):
   - Проверьте Шаг 4 и Шаг 5 для получения фактического выпуска

🔍 Рекомендации:
   1. Если заказ не найден — проверьте правильность номера
   2. Если нет полей в продукции — попробуйте запрос без $select
   3. Для расчёта remaining_qty используйте:
      remaining_qty = ordered_qty (из Шаг 3) - produced_qty (из Шаг 5)
""")


if __name__ == "__main__":
    main()
