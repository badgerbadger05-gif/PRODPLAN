"""Сравнение запросов к 1С"""
import os
import urllib.request, json, base64, urllib.parse

BASE = "http://mtzw7/unf/odata/standard.odata/"
USERNAME = os.getenv("ODATA_USERNAME")
PASSWORD = os.getenv("ODATA_PASSWORD")


def _build_auth_header() -> str | None:
    if not USERNAME or not PASSWORD:
        return None
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


AUTH = _build_auth_header()

def query(name, filter, orderby="Ref_Key", top=1000):
    filter_encoded = urllib.parse.quote(filter, safe="'()")
    url = f"{BASE}Document_ЗаказНаПроизводство?$filter={filter_encoded}&$top={top}&$orderby={orderby}"
    req = urllib.request.Request(url)
    req.add_header('Accept', 'application/json')
    if AUTH:
        req.add_header('Authorization', AUTH)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
        items = data.get('value', [])
        print(f"\n{name}: {len(items)} записей (orderby={orderby})")
        # Считаем префиксы
        prefixes = {}
        for item in items:
            num = item.get('Number', '')
            prefix = num.split('-')[0] if '-' in num else num[:3]
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
        print(f"  Префиксы: {prefixes}")
        print(f"  Первые 5:")
        for item in items[:5]:
            print(f"    {item.get('Number')} (DM={item.get('DeletionMark')}, Posted={item.get('Posted')})")
        return items

# Запрос 1: Как в программе (orderby=Ref_Key)
query("1. Программа (orderby=Ref_Key)", 
      "DeletionMark eq false and Posted eq true",
      orderby="Ref_Key")

# Запрос 2: Как в моём скрипте (orderby=Number)
query("2. Мой скрипт (orderby=Number)", 
      "DeletionMark eq false and Posted eq true",
      orderby="Number")
