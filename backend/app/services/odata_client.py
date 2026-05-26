from __future__ import annotations

import json
import urllib.request
import urllib.parse
import urllib.error
import time
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

    def _make_request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
        retries: int = 4,
        retry_backoff_sec: float = 1.0,
    ) -> Dict[str, Any]:
        endpoint_clean = (endpoint or "").lstrip("/")
        endpoint_quoted = urllib.parse.quote(endpoint_clean, safe="$()_-,.=/'")
        url = f"{self.base_url}/{endpoint_quoted}"
        if params:
            # Кодируем параметры вручную для корректной обработки кириллицы и пробелов
            query_parts = []
            for key, value in params.items():
                key_encoded = urllib.parse.quote(str(key), safe='')
                # value кодируем полностью, затем заменяем пробелы на %20
                value_encoded = urllib.parse.quote(str(value), safe="$,()*'")
                value_encoded = value_encoded.replace(' ', '%20')
                query_parts.append(f"{key_encoded}={value_encoded}")
            query_string = "&".join(query_parts)
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
        # Trace requested URL (no auth data)
        try:
            from datetime import datetime as _dt
            print(f"[OData] {_dt.utcnow().isoformat()} GET {url}")
        except Exception:
            pass
        last_err: Optional[Exception] = None
        for attempt in range(0, max(0, int(retries)) + 1):
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
                # Many 1C / reverse proxies respond with 503/504 under load.
                # Retry a few times with backoff to reduce flakiness of manual sync.
                err_data = ""
                try:
                    err_data = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                retryable = int(getattr(e, "code", 0) or 0) in {429, 500, 502, 503, 504}
                if retryable and attempt < int(retries):
                    # Respect Retry-After when present
                    wait_s: Optional[float] = None
                    try:
                        ra = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
                        if ra:
                            wait_s = float(ra)
                    except Exception:
                        wait_s = None
                    if wait_s is None:
                        wait_s = float(retry_backoff_sec) * (2 ** attempt)
                    time.sleep(max(0.1, min(wait_s, 30.0)))
                    last_err = e
                    continue
                raise urllib.error.URLError(
                    f"HTTP Error {e.code}: {e.reason}. URL: {url}. Details: {err_data}"
                )

            except urllib.error.URLError as e:
                # Network errors may be transient too (DNS/connection reset)
                if attempt < int(retries):
                    time.sleep(max(0.1, min(float(retry_backoff_sec) * (2 ** attempt), 30.0)))
                    last_err = e
                    continue
                raise urllib.error.URLError(f"URL Error: {str(e)}. URL: {url}")

        # Should be unreachable due to raises above
        if last_err:
            raise last_err
        raise urllib.error.URLError(f"Unknown error. URL: {url}")

    def _build_headers(self) -> Dict[str, str]:
        headers = dict(self.default_headers)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.username and self.password:
            import base64

            credentials = f"{self.username}:{self.password}"
            encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    def post(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        timeout: int = 60,
    ) -> Dict[str, Any]:
        endpoint_clean = (endpoint or "").lstrip("/")
        endpoint_quoted = urllib.parse.quote(endpoint_clean, safe="$()_-,.=/'")
        url = f"{self.base_url}/{endpoint_quoted}"
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        for k, v in self._build_headers().items():
            request.add_header(k, v)
        try:
            from datetime import datetime as _dt
            print(f"[OData] {_dt.utcnow().isoformat()} POST {url}")
        except Exception:
            pass
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                text = data.decode("utf-8", errors="replace").strip()
                if not text:
                    return {}
                content_type = response.headers.get("Content-Type", "") or ""
                if "application/json" in content_type.lower() or text.startswith("{") or text.startswith("["):
                    return json.loads(text)
                return {"_raw": text, "_content_type": content_type, "_url": url}
        except urllib.error.HTTPError as e:
            err_data = ""
            try:
                err_data = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise urllib.error.URLError(
                f"HTTP Error {e.code}: {e.reason}. URL: {url}. Details: {err_data}"
            )

    def post_operation(
        self,
        endpoint: str,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        endpoint_clean = (endpoint or "").lstrip("/")
        endpoint_quoted = urllib.parse.quote(endpoint_clean, safe="$()_-,.=/'?&")
        url = f"{self.base_url}/{endpoint_quoted}"
        request = urllib.request.Request(url, data=b"", method="POST")
        for k, v in self._build_headers().items():
            request.add_header(k, v)
        try:
            from datetime import datetime as _dt
            print(f"[OData] {_dt.utcnow().isoformat()} POST {url}")
        except Exception:
            pass
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                text = data.decode("utf-8", errors="replace").strip()
                if not text:
                    return {}
                content_type = response.headers.get("Content-Type", "") or ""
                if "application/json" in content_type.lower() or text.startswith("{") or text.startswith("["):
                    return json.loads(text)
                return {"_raw": text, "_content_type": content_type, "_url": url}
        except urllib.error.HTTPError as e:
            err_data = ""
            try:
                err_data = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise urllib.error.URLError(
                f"HTTP Error {e.code}: {e.reason}. URL: {url}. Details: {err_data}"
            )

    def patch(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        timeout: int = 60,
    ) -> Dict[str, Any]:
        endpoint_clean = (endpoint or "").lstrip("/")
        endpoint_quoted = urllib.parse.quote(endpoint_clean, safe="$()_-,.=/'")
        url = f"{self.base_url}/{endpoint_quoted}"
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="PATCH")
        for k, v in self._build_headers().items():
            request.add_header(k, v)
        try:
            from datetime import datetime as _dt
            print(f"[OData] {_dt.utcnow().isoformat()} PATCH {url}")
        except Exception:
            pass
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                text = data.decode("utf-8", errors="replace").strip()
                if not text:
                    return {}
                content_type = response.headers.get("Content-Type", "") or ""
                if "application/json" in content_type.lower() or text.startswith("{") or text.startswith("["):
                    return json.loads(text)
                return {"_raw": text, "_content_type": content_type, "_url": url}
        except urllib.error.HTTPError as e:
            err_data = ""
            try:
                err_data = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise urllib.error.URLError(
                f"HTTP Error {e.code}: {e.reason}. URL: {url}. Details: {err_data}"
            )

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


def _is_guid_like(value: str) -> bool:
    s = str(value or "").strip().lower()
    if len(s) != 36:
        return False
    parts = s.split("-")
    if len(parts) != 5:
        return False
    sizes = [8, 4, 4, 4, 12]
    for p, n in zip(parts, sizes):
        if len(p) != n:
            return False
        try:
            int(p, 16)
        except Exception:
            return False
    return True


def _extract_ref_key(value: Any) -> str:
    if isinstance(value, dict):
        return str((value.get("Ref_Key") or value.get("RefKey") or value.get("ref_key") or "")).strip()
    return str(value or "").strip()


def _resolve_warehouse_mapping(client: OData1CClient, warehouse_refs: List[str]) -> Dict[str, Dict[str, str]]:
    """
    Пытается резолвить склады по GUID через наиболее типовые каталоги 1С.
    Возвращает map: Ref_Key -> {"Code": "...", "Name": "..."}.
    """
    refs = sorted({str(x).strip() for x in (warehouse_refs or []) if str(x).strip()})
    if not refs:
        return {}
    guid_refs = [r for r in refs if _is_guid_like(r)]
    if not guid_refs:
        return {}

    # В разных конфигурациях 1С склады могут лежать в разных каталогах.
    # Пробуем типовые варианты и тихо пропускаем отсутствующие.
    candidate_entities = [
        "Catalog_Склады",
        "Catalog_СтруктурныеЕдиницы",
        "Catalog_СтруктурныеЕдиницыПредприятия",
        "Catalog_СкладыПредприятия",
    ]

    mapping: Dict[str, Dict[str, str]] = {}
    chunk_size = 20
    select_fields = "Ref_Key,Code,Description"

    for entity in candidate_entities:
        unresolved = [r for r in guid_refs if r not in mapping]
        if not unresolved:
            break
        try:
            for i in range(0, len(unresolved), chunk_size):
                chunk = unresolved[i:i + chunk_size]
                ors = " or ".join([f"Ref_Key eq guid'{k}'" for k in chunk])
                resp = client._make_request(
                    entity,
                    {"$select": select_fields, "$filter": f"({ors})"},
                )
                rows: List[Dict[str, Any]] = []
                if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
                    rows = resp["value"]
                elif isinstance(resp, dict):
                    rows = [resp]
                for row in rows:
                    rk = str(row.get("Ref_Key") or "").strip()
                    if not rk:
                        continue
                    code = str(
                        row.get("Code")
                        or row.get("Код")
                        or ""
                    ).strip()
                    name = str(
                        row.get("Description")
                        or row.get("Наименование")
                        or row.get("Name")
                        or ""
                    ).strip()
                    if rk not in mapping:
                        mapping[rk] = {"Code": code, "Name": name}
                    else:
                        if not mapping[rk].get("Code") and code:
                            mapping[rk]["Code"] = code
                        if not mapping[rk].get("Name") and name:
                            mapping[rk]["Name"] = name
        except Exception:
            continue

    return mapping


def convert_1c_stock_to_records(
    stock_data: List[Dict[str, Any]],
    key_to_code: Optional[Dict[str, Dict[str, str]]] = None,
    key_field_name: str = "Номенклатура_Key",
    warehouse_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for record in stock_data:
        nomenclature = record.get("Номенклатура", {}) or {}
        # Приоритет для сопоставления с нашей БД: сначала Code/Код (типичный код номенклатуры вида 00-0000...), затем Артикул
        item_code = (
            nomenclature.get("Code")
            or nomenclature.get("Код")
            or record.get("Code")
            or record.get("Код")
            or nomenclature.get("Артикул")
            or record.get("Артикул")
        )
        item_name = nomenclature.get("Наименование") or record.get("Наименование")
        if not item_code:
            ref_key = record.get(key_field_name)
            # Пытаемся получить Ref_Key номенклатуры из разных вариантов представления
            ref_key_val = (
                record.get(key_field_name)
                or record.get("НоменклатураRef_Key")
                or record.get("Номенклатура_Ref_Key")
                or record.get("Номенклатура")
            )
            ref_key = _extract_ref_key(ref_key_val)
    
            # Если есть сопоставление по Ref_Key — всегда берём код из каталога (стабильнее для сопоставления)
            if key_to_code and ref_key and ref_key in key_to_code:
                m = key_to_code[ref_key]
                # Предпочитаем Code/Код (типичные коды вида 00-0000...), затем Артикул
                item_code = (m.get("Code") or m.get("Артикул") or item_code)
                if not item_name:
                    item_name = m.get("Description") or item_name
        qty = None
        # Расширяем возможные имена полей кол-ва для Balance и вариаций остатков
        # Пример ответа Balance: "КоличествоBalance", "КоличествоИнтBalance"
        for qf in [
            "КоличествоBalance",
            "КоличествоИнтBalance",
            "Количество",
            "Остаток",
            "КоличествоОстаток",
            "КоличествоОстатка",
            "КоличествоНаСкладе",
            "ОстатокКоличество",
            "КоличествоКонечныйОстаток",
            "КонечныйОстатокКоличество",
            "ОстатокНаКонецКоличество",
            "Quantity",
            "Qty"
        ]:
            if qf in record and record.get(qf) is not None:
                try:
                    qty = float(record.get(qf) or 0.0)
                    break
                except Exception:
                    continue
        if qty is None:
            qty = 0.0

        # Извлечём склад (СтруктурнаяЕдиница) для фильтрации остатков по выбранным складам
        warehouse_raw = (
            record.get("СтруктурнаяЕдиница")
            or record.get("Склад")
            or {}
        )
        warehouse_ref = (
            record.get("СтруктурнаяЕдиница_Key")
            or record.get("Склад_Key")
            or record.get("СтруктурнаяЕдиницаRef_Key")
            or record.get("СкладRef_Key")
        )
        warehouse_code = None
        warehouse_name = None

        if isinstance(warehouse_raw, dict):
            if not warehouse_ref:
                warehouse_ref = _extract_ref_key(warehouse_raw)
            warehouse_code = (
                warehouse_raw.get("Code")
                or warehouse_raw.get("Код")
            )
            warehouse_name = (
                warehouse_raw.get("Description")
                or warehouse_raw.get("Наименование")
                or warehouse_raw.get("Name")
            )
        elif isinstance(warehouse_raw, str) and warehouse_raw.strip():
            warehouse_name = warehouse_raw.strip()

        warehouse_ref = _extract_ref_key(warehouse_ref)
        wm = warehouse_map.get(warehouse_ref) if (warehouse_map and warehouse_ref) else None
        if wm:
            mapped_code = str(wm.get("Code") or "").strip()
            mapped_name = str(wm.get("Name") or "").strip()
            if not warehouse_code and mapped_code:
                warehouse_code = mapped_code
            # Если name отсутствует, совпадает с GUID или тоже выглядит как GUID — подменим на справочное.
            if mapped_name and (not warehouse_name or warehouse_name == warehouse_ref or _is_guid_like(warehouse_name)):
                warehouse_name = mapped_name

        # Извлечём Ref_Key для возврата (поможет сопоставлять по GUID)
        ref_out_val = (
            record.get(key_field_name)
            or record.get("НоменклатураRef_Key")
            or record.get("Номенклатура_Ref_Key")
            or record.get("Номенклатура")
        )
        ref_out = _extract_ref_key(ref_out_val)

        converted.append({
            "code": str(item_code).strip() if item_code else "",
            "name": str(item_name).strip() if item_name else "",
            "qty": qty,
            "ref": ref_out,
            "warehouse_ref": warehouse_ref,
            "warehouse_code": str(warehouse_code).strip() if warehouse_code else "",
            "warehouse_name": str(warehouse_name).strip() if warehouse_name else "",
        })
    return converted


def get_stock_from_1c_odata(
    base_url: str,
    entity_name: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None,
    filter_query: Optional[str] = None,
    select_fields: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Универсальная загрузка остатков.
    Особенность: для AccumulationRegister .../Balance у 1С может не быть поля Ref_Key,
    поэтому $orderby по умолчанию отключаем.
    """
    client = OData1CClient(base_url, username, password, token)

    # The plain AccumulationRegister_ЗапасыНаСкладах entity returns movement
    # recorders with nested RecordSet lines in UNF demo. For stock sync we need
    # the actual current balance, so use the virtual Balance table by default.
    if (
        "/Balance" not in (entity_name or "")
        and str(entity_name or "").strip().startswith("AccumulationRegister_ЗапасыНаСкладах")
        and not filter_query
        and not select_fields
    ):
        entity_name = str(entity_name).strip().rstrip("/") + "/Balance"

    # Для Balance отключаем $orderby, иначе 1С может вернуть пусто/ошибку
    use_order_by: Optional[str] = None if "/Balance" in (entity_name or "") else "Ref_Key"

    # Специальная обработка Balance: Period как параметр функции, а не через $filter
    effective_entity = entity_name
    eff_filter = filter_query
    if "/Balance" in (entity_name or ""):
        try:
            import re
            from datetime import datetime
            period_val = None
            if eff_filter:
                m = re.search(r"Period\s+le\s+datetime'([^']+)'", str(eff_filter))
                if m:
                    period_val = m.group(1)
            if not period_val:
                period_val = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            # По умолчанию запрашиваем баланс в разрезе Номенклатуры, Склада (СтруктурнаяЕдиница) и Организации
            dims = "Номенклатура,СтруктурнаяЕдиница,Организация"
            effective_entity = f"{str(entity_name).strip().rstrip('/')}(Period=datetime'{period_val}',Dimensions='{dims}')"
            eff_filter = None
        except Exception:
            # В случае сбоя оставим исходные значения — запрос либо вернёт пусто, либо 1С подскажет ошибку
            pass

    stock_data = client.get_all(
        entity_name=effective_entity,
        filter_query=eff_filter,
        select_fields=select_fields,
        top=1000,
        max_records=None,
        max_pages=1000,
        order_by=use_order_by,
    )

    # Сбор ключей номенклатуры из ответа Balance (вариативные поля)
    keys: List[str] = []
    for r in stock_data:
        k = (
            r.get("Номенклатура_Key")
            or r.get("НоменклатураRef_Key")
            or r.get("Номенклатура_Ref_Key")
            or r.get("Номенклатура")
        )
        if k is None:
            continue
        try:
            if isinstance(k, dict):
                ks = str((k.get("Ref_Key") or k.get("RefKey") or k.get("ref_key") or "").strip())
            else:
                ks = str(k).strip()
            if ks:
                keys.append(ks)
        except Exception:
            continue
    keys = sorted({k for k in keys if k})
    key_to_code: Optional[Dict[str, Dict[str, str]]] = None
    if not keys:
        try:
            # Debug-помощь: покажем доступные поля первой записи
            if stock_data:
                sample = stock_data[0]
                print("[OData] Balance sample fields:", list(sample.keys())[:20])
        except Exception:
            pass
    if keys:
        try:
            # Получаем соответствия Ref_Key -> (Code, Description) батчами
            mapping: Dict[str, Dict[str, str]] = {}
            CHUNK = 20
            for i in range(0, len(keys), CHUNK):
                chunk = keys[i:i + CHUNK]
                ors = " or ".join([f"Ref_Key eq guid'{k}'" for k in chunk])
                resp = client._make_request(
                    "Catalog_Номенклатура",
                    {"$select": "Ref_Key,Code,Description,Артикул", "$filter": f"({ors})"}
                )
                rows = []
                if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
                    rows = resp["value"]
                elif resp:
                    rows = [resp]
                for r in rows:
                    rk = str(r.get("Ref_Key") or "").strip()
                    if rk:
                        mapping[rk] = {
                            "Code": str(r.get("Code") or "").strip(),
                            "Description": str(r.get("Description") or "").strip(),
                            "Артикул": str(r.get("Артикул") or "").strip(),
                        }
            key_to_code = mapping
        except Exception:
            key_to_code = None

    warehouse_keys: List[str] = []
    for r in stock_data:
        w = (
            r.get("СтруктурнаяЕдиница_Key")
            or r.get("Склад_Key")
            or r.get("СтруктурнаяЕдиницаRef_Key")
            or r.get("СкладRef_Key")
            or r.get("СтруктурнаяЕдиница")
            or r.get("Склад")
        )
        wk = _extract_ref_key(w)
        if wk:
            warehouse_keys.append(wk)
    warehouse_map = _resolve_warehouse_mapping(client, warehouse_keys)

    return convert_1c_stock_to_records(
        stock_data,
        key_to_code=key_to_code,
        warehouse_map=warehouse_map,
    )

# Bind helper as a method of the client class for runtime (keeps API backward-compatible)
OData1CClient.iter_by_guid = iter_by_guid
