import urllib.request
import urllib.parse
import json
import base64
import os

# Кодируем имя сущности
entity = urllib.parse.quote("AccumulationRegister_ЗаказыНаПроизводство/Balance", safe="/")
base_url = f"http://mtzw7/unf/odata/standard.odata/{entity}"

params = {
    "$format": "json",
    "$top": "10",
    "$filter": "ЗаказНаПроизводство_Key eq guid'2fafcff0-e787-11ef-829a-9ee51454587f'"
}

# Кодируем параметры
query_parts = []
for key, value in params.items():
    key_encoded = urllib.parse.quote(str(key), safe='')
    value_encoded = urllib.parse.quote(str(value), safe="$,()*'")
    query_parts.append(f"{key_encoded}={value_encoded}")
query_string = "&".join(query_parts)
full_url = f"{base_url}?{query_string}"

print(f"URL: {full_url}")

req = urllib.request.Request(full_url)
username = os.getenv("ODATA_USERNAME")
password = os.getenv("ODATA_PASSWORD")
if username and password:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")

try:
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    print(f"Found: {len(data.get('value', []))} records")
    for rec in data.get('value', []):
        print(json.dumps(rec, ensure_ascii=False, indent=2))
except Exception as e:
    print(f"Error: {e}")
