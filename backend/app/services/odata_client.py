from __future__ import annotations

import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Optional, Any


class OData1CClient:
    def __init__(self, base_url: str, username: Optional[str] = None, password: Optional[str] = None, token: Optional[str] = None):
        u = (base_url or "").strip().rstrip("/")
        if u.lower().endswith("$metadata"):
            u = u[: -len("$metadata")].rstrip("/")
        self.base_url = u
        self.username = username
        self.password = password
        self.token = token
        self.default_headers = {
            "Accept": "application/json;odata.metadata=minimal",
            "Content-Type": "application/json",
        }

    def _make_request(self, endpoint: str, params: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Dict[str, Any]:
        endpoint_clean = (endpoint or "").lstrip("/")
        endpoint_quoted = urllib.parse.quote(endpoint_clean, safe="$()_-,.=/'")
        url = f"{self.base_url}/{endpoint_quoted}"
        if params:
            query_string = urllib.parse.urlencode(params, doseq=True, safe="/$,()'", encoding="utf-8")
            url = f"{url}?{query_string}"
        request = urllib.request.Request(url)
        for k, v in self.default_headers.items():
            request.add_header(k, v)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        elif self.username and self.password:
            import base64
            credentials = f"{self.username}:{self.password}"
            encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
            request.add_header("Authorization", f"Basic {encoded}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                try:
                    content_type = response.headers.get("Content-Type", "") or ""
                except Exception:
                    content_type = ""
                text = data.decode("utf-8", errors="replace").strip()
                if "application/json" in content_type.lower() or text.startswith("{") or text.startswith("["):
                    return json.loads(text)
                return {"_raw": text, "_content_type": content_type, "_url": url}
        except urllib.error.HTTPError as e:
            err_data = ""
            try:
                err_data = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise urllib.error.URLError(f"HTTP Error {e.code}: {e.reason}. URL: {url}. Details: {err_data}")
        except urllib.error.URLError as e:
            raise urllib.error.URLError(f"URL Error: {str(e)}. URL: {url}")

    @staticmethod
    def _sanitize_select_fields(select_fields: Optional[List[str]]) -> Optional[List[str]]:
        if not select_fields:
            return None
        out: List[str] = []
        for f in select_fields:
            name = str(f or "").strip()
            if not name or "/" in name:
                continue
            out.append(name)
        return out or None

    def get_all(
        self,
        entity_name: str,
        filter_query: Optional[str] = None,
        select_fields: Optional[List[str]] = None,
        top: int = 1000,
        max_records: Optional[int] = None,
        max_pages: int = 1000,
        order_by: Optional[str] = "Ref_Key",
    ) -> List[Dict[str, Any]]:
        """
        Универсальная постраничная выборка из OData.

        Изменения:
        - Убрано жёсткое ограничение 50 000 записей (ранее могло обрезать номенклатуру).
        - Добавлены параметры max_records и max_pages для защиты от бесконечных циклов.
        - Добавлен параметр order_by (по умолчанию Ref_Key) для стабильной пагинации 1С OData.
        """
        all_data: List[Dict[str, Any]] = []
        skip = 0
        last_sig: Optional[str] = None
        page_count = 0

        while True:
            # Ограничение на количество страниц (страховка от бесконечного цикла)
            page_count += 1
            if max_pages and page_count > max_pages:
                break

            params: Dict[str, Any] = {"$top": top, "$skip": skip}
            if filter_query:
                params["$filter"] = filter_query
            sanitized = self._sanitize_select_fields(select_fields)
            if sanitized:
                params["$select"] = ",".join(sanitized)
            if order_by:
                params["$orderby"] = order_by

            result = self._make_request(entity_name, params)

            if isinstance(result, dict) and "value" in result:
                data = result["value"]
                if not data:
                    break

                all_data.extend(data)

                # Если задан верхний предел записей — соблюдаем его
                if max_records is not None and len(all_data) >= max_records:
                    break

                # Если страница меньше top — данных больше нет
                if len(data) < top:
                    break

                # Сигнатура страницы для защиты от зацикливания на одинаковых страницах
                try:
                    head = data[:3] if isinstance(data, list) else []
                    sig = f"{len(data)}|{json.dumps(head, ensure_ascii=False, sort_keys=True)}"
                except Exception:
                    sig = f"{len(data)}"

                if last_sig is not None and sig == last_sig:
                    break
                last_sig = sig

                # Переход на следующую страницу
                skip += len(data)
            else:
                if result:
                    all_data.append(result)
                break

        return all_data

    def get_count(self, entity_name: str, filter_query: Optional[str] = None) -> int:
        """
        Возвращает количество записей в сущности 1С OData.
        Использует endpoint Entity/$count (text/plain).
        """
        endpoint = f"{entity_name.strip().lstrip('/')}/$count"
        params: Dict[str, Any] = {}
        if filter_query:
            params["$filter"] = filter_query
        try:
            result = self._make_request(endpoint, params)
            # _make_request вернёт dict c _raw для text/plain
            if isinstance(result, dict):
                raw = str(result.get("_raw", "")).strip()
                if raw.isdigit():
                    return int(raw)
                # иногда 1С может вернуть число в JSON
                if "value" in result and str(result["value"]).isdigit():
                    return int(result["value"])
            # если по каким-то причинам пришёл не dict
            try:
                return int(str(result).strip())
            except Exception:
                return 0
        except Exception:
            return 0

    def iter_pages(
        self,
        entity_name: str,
        filter_query: Optional[str] = None,
        select_fields: Optional[List[str]] = None,
        top: int = 1000,
        max_pages: int = 1000,
        order_by: Optional[str] = "Ref_Key",
    ):
        """
        Итератор по страницам результата OData. На каждой итерации возвращает список записей (страницу).
        """
        skip = 0
        page_count = 0
        last_sig: Optional[str] = None

        while True:
            page_count += 1
            if max_pages and page_count > max_pages:
                break

            params: Dict[str, Any] = {"$top": top, "$skip": skip}
            if filter_query:
                params["$filter"] = filter_query
            sanitized = self._sanitize_select_fields(select_fields)
            if sanitized:
                params["$select"] = ",".join(sanitized)
            if order_by:
                params["$orderby"] = order_by

            result = self._make_request(entity_name, params)
            if isinstance(result, dict) and "value" in result:
                data = result["value"] or []
                if not data:
                    break

                yield data

                # Защита от зацикливания
                try:
                    head = data[:3] if isinstance(data, list) else []
                    sig = f"{len(data)}|{json.dumps(head, ensure_ascii=False, sort_keys=True)}"
                except Exception:
                    sig = f"{len(data)}"

                if last_sig is not None and sig == last_sig:
                    break
                last_sig = sig

                if len(data) < top:
                    break
                skip += len(data)
            else:
                # Неожиданный ответ — считаем как одна "страница"
                if result:
                    yield [result]
                break


def iter_by_guid(
    self,
    entity_name: str,
    key_field: str = "Ref_Key",
    filter_query: Optional[str] = None,
    select_fields: Optional[List[str]] = None,
    top: int = 1000,
    max_pages: int = 10000,
):
    """
    Ключевая постраничная выборка по GUID-ключу (например, Ref_Key) для 1С OData.

    Алгоритм:
    - $orderby key_field
    - при наличии last_key: добавляем к исходному фильтру условие key_field gt guid'last_key'
    - идём батчами top, пока страница неполная
    """
    last_key: Optional[str] = None
    page_count = 0

    while True:
        page_count += 1
        if max_pages and page_count > max_pages:
            break

        filters: List[str] = []
        if filter_query:
            filters.append(f"({filter_query})")
        if last_key:
            filters.append(f"{key_field} gt guid'{last_key}'")
        combined_filter = " and ".join(filters) if filters else None

        params: Dict[str, Any] = {"$top": top, "$orderby": key_field}
        if combined_filter:
            params["$filter"] = combined_filter

        sanitized = self._sanitize_select_fields(select_fields)
        if sanitized:
            params["$select"] = ",".join(sanitized)

        result = self._make_request(entity_name, params)
        if isinstance(result, dict) and "value" in result:
            data = result["value"] or []
            if not data:
                break

            yield data

            if len(data) < top:
                break

            # Запоминаем последний ключ
            try:
                last = data[-1]
                last_key = str((last.get(key_field) or "")).strip()
                if not last_key:
                    break
            except Exception:
                break
        else:
            # Неожиданный ответ — считаем как одна "страница"
            if result:
                yield [result]
            break


def convert_1c_stock_to_records(stock_data: List[Dict[str, Any]], key_to_code: Optional[Dict[str, Dict[str, str]]] = None, key_field_name: str = "Номенклатура_Key") -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for record in stock_data:
        nomenclature = record.get("Номенклатура", {}) or {}
        item_code = nomenclature.get("Артикул") or nomenclature.get("Код") or record.get("Код")
        item_name = nomenclature.get("Наименование") or record.get("Наименование")
        if not item_code:
            ref_key = record.get(key_field_name)
            if key_to_code and ref_key and ref_key in key_to_code:
                item_code = key_to_code[ref_key].get("Code") or item_code
                if not item_name:
                    item_name = key_to_code[ref_key].get("Description") or item_name
        qty = None
        for qf in ["Количество","Остаток","КоличествоОстаток","КоличествоОстатка","КоличествоНаСкладе","ОстатокКоличество","Quantity","Qty"]:
            if qf in record and record.get(qf) is not None:
                try:
                    qty = float(record.get(qf) or 0.0)
                    break
                except Exception:
                    continue
        if qty is None:
            qty = 0.0
        if item_code:
            converted.append({"code": str(item_code).strip(), "name": str(item_name).strip() if item_name else "", "qty": qty})
    return converted


def get_stock_from_1c_odata(base_url: str, entity_name: str, username: Optional[str] = None, password: Optional[str] = None, token: Optional[str] = None, filter_query: Optional[str] = None, select_fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    client = OData1CClient(base_url, username, password, token)
    stock_data = client.get_all(entity_name, filter_query, select_fields)
    keys = [str(r.get("Номенклатура_Key")).strip() for r in stock_data if r.get("Номенклатура_Key")]
    keys = sorted({k for k in keys if k})
    key_to_code: Optional[Dict[str, Dict[str, str]]] = None
    if keys:
        try:
            # Fetch names by keys in small batches similar to legacy client
            # Build or-predicate Ref_Key eq guid'...'
            # We'll call internal method via public API of client
            mapping: Dict[str, Dict[str, str]] = {}
            CHUNK = 20
            for i in range(0, len(keys), CHUNK):
                chunk = keys[i:i+CHUNK]
                ors = " or ".join([f"Ref_Key eq guid'{k}'" for k in chunk])
                resp = client._make_request("Catalog_Номенклатура", {"$select": "Ref_Key,Code,Description", "$filter": f"({ors})"})
                rows = []
                if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
                    rows = resp["value"]
                elif resp:
                    rows = [resp]
                for r in rows:
                    rk = str(r.get("Ref_Key") or "").strip()
                    if rk:
                        mapping[rk] = {"Code": str(r.get("Code") or "").strip(), "Description": str(r.get("Description") or "").strip()}
            key_to_code = mapping
        except Exception:
            key_to_code = None
    return convert_1c_stock_to_records(stock_data, key_to_code=key_to_code)

# Bind helper as a method of the client class for runtime (keeps API backward-compatible)
OData1CClient.iter_by_guid = iter_by_guid