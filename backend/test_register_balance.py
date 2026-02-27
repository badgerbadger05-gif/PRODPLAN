"""
Скрипт для исследования регистра AccumulationRegister_ЗаказыНаПроизводство через OData.
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import base64
import os

BASE_URL = "http://mtzw7/unf/odata/standard.odata/"
USERNAME = os.getenv("ODATA_USERNAME")
PASSWORD = os.getenv("ODATA_PASSWORD")

def make_request(endpoint, params=None):
    """Выполняет GET запрос к 1С OData."""
    endpoint_encoded = urllib.parse.quote(endpoint, safe="/")
    url = f"{BASE_URL}{endpoint_encoded}"
    if params:
        query_parts = []
        for key, value in params.items():
            key_encoded = urllib.parse.quote(str(key), safe='')
            value_encoded = urllib.parse.quote(str(value), safe="$,()*'")
            query_parts.append(f"{key_encoded}={value_encoded}")
        query_string = "&".join(query_parts)
        url = f"{url}?{query_string}"

    print(f"\n{'='*80}")
    print(f"URL: {url.replace(' ', '%20')}")
    print('='*80)

    url = url.replace(' ', '%20')

    request = urllib.request.Request(url)
    request.add_header("Accept", "application/json;odata.metadata=minimal")

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
    except Exception as e:
        print(f"Error: {e}")
        return None


def print_result(title, result, max_items=5):
    """Красиво выводит результат."""
    print(f"\n{'#'*80}")
    print(f"# {title}")
    print('#'*80)

    if result is None:
        print("Result: None")
        return

    if isinstance(result, dict):
        if "value" in result:
            items = result["value"]
            print(f"Found records: {len(items)}")
            if items:
                print(f"\nFirst {min(max_items, len(items))} records:")
                for i, item in enumerate(items[:max_items]):
                    print(f"\n--- Record {i+1} ---")
                    print(json.dumps(item, ensure_ascii=False, indent=2))
        else:
            print("Result (dict):")
            print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])
    elif isinstance(result, list):
        print(f"Found records: {len(result)}")
        for i, item in enumerate(result[:max_items]):
            print(f"\n--- Record {i+1} ---")
            print(json.dumps(item, ensure_ascii=False, indent=2))
    else:
        print(f"Result: {result}")


if __name__ == "__main__":
    print("="*80)
    print("ИССЛЕДОВАНИЕ РЕГИСТРА ЗаказыНаПроизводство")
    print("="*80)

    # =====================================================================
    # Шаг 1: Проверка структуры регистра (Balance)
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 1: Структура регистра (Balance)")
    print("="*80)
    
    result = make_request(
        "AccumulationRegister_ЗаказыНаПроизводство/Balance",
        {
            "$format": "json",
            "$top": "10"
        }
    )
    print_result("Шаг 1: Balance (первые 10 записей)", result)

    # =====================================================================
    # Шаг 2: Balance с конкретными измерениями
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 2: Balance с измерениями")
    print("="*80)
    
    result = make_request(
        "AccumulationRegister_ЗаказыНаПроизводство/Balance",
        {
            "$format": "json",
            "$top": "10",
            "$select": "Номенклатура_Key,Организация_Key,ЗаказНаПроизводство_Key,КоличествоBalance,КоличествоПриход,КоличествоРасход"
        }
    )
    print_result("Шаг 2: Balance с полями", result)

    # =====================================================================
    # Шаг 3: Balance с фильтром по конкретному заказу (ЗСНФ-000854)
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 3: Balance для заказа ЗСНФ-000854")
    print("="*80)
    
    # Сначала найдём Ref_Key заказа
    order_result = make_request(
        "Document_ЗаказНаПроизводство",
        {
            "$format": "json",
            "$select": "Ref_Key,Number",
            "$filter": "Number eq 'ЗСНФ-000854'"
        }
    )
    
    order_ref_key = None
    if order_result and "value" in order_result and order_result["value"]:
        order_ref_key = order_result["value"][0].get("Ref_Key")
        print(f"\nRef_Key for order ZSNF-000854: {order_ref_key}")
    
    if order_ref_key:
        result = make_request(
            "AccumulationRegister_ЗаказыНаПроизводство/Balance",
            {
                "$format": "json",
                "$select": "Номенклатура_Key,Организация_Key,ЗаказНаПроизводство_Key,КоличествоBalance,КоличествоПриход,КоличествоРасход",
                "$filter": f"ЗаказНаПроизводство_Key eq guid'{order_ref_key}'"
            }
        )
        print_result("Step 3: Balance for ZSNF-000854", result, max_items=20)

    # =====================================================================
    # Шаг 4: RecordType (движения регистра)
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 4: RecordType (движения регистра)")
    print("="*80)
    
    result = make_request(
        "AccumulationRegister_ЗаказыНаПроизводство.RecordType",
        {
            "$format": "json",
            "$top": "20",
            "$orderby": "Period desc"
        }
    )
    print_result("Шаг 4: RecordType (последние 20 записей)", result)

    # =====================================================================
    # Шаг 5: RecordType с полями
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 5: RecordType с полями")
    print("="*80)
    
    result = make_request(
        "AccumulationRegister_ЗаказыНаПроизводство.RecordType",
        {
            "$format": "json",
            "$top": "20",
            "$orderby": "Period desc",
            "$select": "Period,Recorder,Recorder_Type,Номенклатура_Key,Характеристика_Key,ЗаказНаПроизводство_Key,Количество,ВидДвижения"
        }
    )
    print_result("Шаг 5: RecordType с полями", result)

    # =====================================================================
    # Шаг 6: Сравнение с Document_СборкаЗапасов для ЗСНФ-000854
    # =====================================================================
    print("\n\n" + "="*80)
    print("ШАГ 6: Сборка запасов для ЗСНФ-000854")
    print("="*80)
    
    if order_ref_key:
        result = make_request(
            "Document_СборкаЗапасов",
            {
                "$format": "json",
                "$select": "Ref_Key,Number,Date,ЗаказНаПроизводство_Key",
                "$filter": f"ЗаказНаПроизводство_Key eq guid'{order_ref_key}'"
            }
        )
        print_result("Шаг 6: Сборка запасов (шапки)", result)
        
        assembly_keys = []
        if result and "value" in result:
            assembly_keys = [item.get("Ref_Key") for item in result["value"] if item.get("Ref_Key")]
            print(f"\nFound assemblies: {len(assembly_keys)}")
        
        if assembly_keys:
            or_filter = " or ".join([f"Ref_Key eq guid'{k}'" for k in assembly_keys[:50]])
            result = make_request(
                "Document_СборкаЗапасов_Продукция",
                {
                    "$format": "json",
                    "$select": "Ref_Key,LineNumber,Номенклатура_Key,Характеристика_Key,Количество,Спецификация_Key",
                    "$filter": f"({or_filter})"
                }
            )
            print_result("Step 6: Assembly products", result, max_items=20)

    print("\n\n" + "="*80)
    print("RESEARCH COMPLETED")
    print("="*80)
