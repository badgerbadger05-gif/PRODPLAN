from __future__ import annotations

import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Optional, Any
from pathlib import Path


class OData1CClient:
    """
    Клиент для работы с OData API 1С.
    """
    
    def __init__(self, base_url: str, username: Optional[str] = None, password: Optional[str] = None,
                 token: Optional[str] = None):
        """
        Инициализация клиента OData.
        
        Args:
            base_url: Базовый URL OData сервиса 1С (без завершающего $metadata)
            username: Имя пользователя для Basic аутентификации
            password: Пароль для Basic аутентификации
            token: Токен для Bearer аутентификации
        """
        # Нормализация base_url:
        # - убираем конечный "/$metadata" если его по ошибке указали как базовый URL
        u = (base_url or "").strip().rstrip("/")
        if u.lower().endswith("$metadata"):
            u = u[: -len("$metadata")].rstrip("/")
        self.base_url = u

        self.username = username
        self.password = password
        self.token = token
        
        # Установка заголовков по умолчанию
        self.default_headers = {
            'Accept': 'application/json;odata.metadata=minimal',
            'Content-Type': 'application/json'
        }
    
    def _make_request(self, endpoint: str, params: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Dict[str, Any]:
        """
        Выполнить GET запрос к OData сервису.
        
        Args:
            endpoint: Конечная точка API
            params: Параметры запроса
            
        Returns:
            Результат запроса в формате JSON
            
        Raises:
            urllib.error.URLError: При ошибках запроса
        """
        # Формируем URL с безопасным экранированием не-ASCII в пути и корректным кодированием параметров
        endpoint_clean = (endpoint or "").lstrip("/")
        # Разрешим OData-специальные символы в пути ($, (), ', /, запятая и т.д.)
        endpoint_quoted = urllib.parse.quote(endpoint_clean, safe="$()_-,.=/'")
        url = f"{self.base_url}/{endpoint_quoted}"
        if params:
            # Кодируем параметры запроса, сохраняя часть специальных символов OData
            query_string = urllib.parse.urlencode(params, doseq=True, safe="/$,()'", encoding="utf-8")
            url = f"{url}?{query_string}"
        
        # Создаем запрос
        request = urllib.request.Request(url)
        
        # Добавляем заголовки
        for key, value in self.default_headers.items():
            request.add_header(key, value)
        
        # Настройка аутентификации
        if self.token:
            request.add_header('Authorization', f'Bearer {self.token}')
        elif self.username and self.password:
            import base64
            credentials = f"{self.username}:{self.password}"
            encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
            request.add_header('Authorization', f'Basic {encoded_credentials}')
        
        # Выполняем запрос
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                # Определяем тип контента и пытаемся корректно разобрать ответ
                content_type = ""
                try:
                    content_type = response.headers.get('Content-Type', '') or ""
                except Exception:
                    content_type = ""
                text = data.decode('utf-8', errors='replace').strip()

                # Если это JSON (по заголовку или по формату текста) — парсим
                if 'application/json' in content_type.lower() or text.startswith('{') or text.startswith('['):
                    return json.loads(text)

                # Иначе возвращаем "сырой" ответ в словаре, чтобы вызывающая сторона могла трактовать
                # Это покрывает, например, $metadata, который обычно отдаётся в XML/EDMX
                return {
                    "_raw": text,
                    "_content_type": content_type,
                    "_url": url,
                }
        except urllib.error.HTTPError as e:
            # Читаем тело ошибки для лучшей диагностики
            error_data = ""
            try:
                error_data = e.read().decode('utf-8', errors='replace')
            except Exception:
                pass
            raise urllib.error.URLError(f"HTTP Error {e.code}: {e.reason}. URL: {url}. Details: {error_data}")
        except urllib.error.URLError as e:
            raise urllib.error.URLError(f"URL Error: {str(e)}. URL: {url}")
    def _sanitize_select_fields(self, select_fields: Optional[List[str]]) -> Optional[List[str]]:
        """
        Удалить из $select вложенные пути (field/subfield), т.к. не все сущности поддерживают навигацию/expand.
        Возвращает только верхнеуровневые имена полей.
        """
        if not select_fields:
            return None
        out: List[str] = []
        for f in select_fields:
            try:
                name = str(f or "").strip()
                if not name or "/" in name:
                    continue
                out.append(name)
            except Exception:
                continue
        return out or None

    def get_nomenclature_codes(self, keys: List[str]) -> Dict[str, Dict[str, str]]:
        """
        По списку Ref_Key (GUID) каталога Номенклатура вернуть словарь:
          { Ref_Key: { "Code": ..., "Description": ... } }
        Делает батч-запросы к Catalog_Номенклатура с $filter по порциям.
        """
        result: Dict[str, Dict[str, str]] = {}
        if not keys:
            return result
        # Уникальные ключи
        uniq_keys = sorted({str(k).strip() for k in keys if str(k).strip()})
        if not uniq_keys:
            return result

        CHUNK = 20
        for i in range(0, len(uniq_keys), CHUNK):
            chunk = uniq_keys[i:i + CHUNK]
            # Формируем фильтр вида: (Ref_Key eq guid'...' or Ref_Key eq guid'...')
            ors = " or ".join([f"Ref_Key eq guid'{k}'" for k in chunk])
            params: Dict[str, Any] = {
                "$select": "Ref_Key,Code,Description",
                "$filter": f"({ors})"
            }
            resp = self._make_request("Catalog_Номенклатура", params)
            rows: List[Dict[str, Any]] = []
            if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
                rows = resp["value"]
            elif resp:
                rows = [resp]
            for r in rows:
                try:
                    rk = str(r.get("Ref_Key") or "").strip()
                    if rk:
                        result[rk] = {
                            "Code": str(r.get("Code") or "").strip(),
                            "Description": str(r.get("Description") or "").strip(),
                        }
                except Exception:
                    continue

        return result
    
    def get_stock_data(self, entity_name: str, filter_query: Optional[str] = None,
                       select_fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Получить данные об остатках из 1С через OData.
        
        Args:
            entity_name: Имя сущности OData (например, 'ОстаткиНоменклатуры')
            filter_query: Фильтр OData (например, "ДатаОстатка eq datetime'2023-01-01'")
            select_fields: Список полей для выборки
            
        Returns:
            Список записей об остатках
        """
        params: Dict[str, Any] = {}
        
        # Добавляем $filter если задан
        if filter_query:
            params['$filter'] = filter_query
            
        # Добавляем $select если задан (без вложенных путей)
        sanitized = self._sanitize_select_fields(select_fields)
        if sanitized:
            params['$select'] = ','.join(sanitized)
        
        # Получаем данные
        result = self._make_request(f"{entity_name}", params)
        
        # Извлекаем записи из результата
        if 'value' in result:
            return result['value']
        else:
            return [result] if result else []
    
    def get_all_stock_data(self, entity_name: str, filter_query: Optional[str] = None,
                           select_fields: Optional[List[str]] = None,
                           top: int = 1000) -> List[Dict[str, Any]]:
        """
        Получить все данные об остатках с учетом пагинации.
        
        Args:
            entity_name: Имя сущности OData
            filter_query: Фильтр OData
            select_fields: Список полей для выборки
            top: Количество записей на страницу
            
        Returns:
            Все записи об остатках
        """
        all_data: List[Dict[str, Any]] = []
        skip = 0
        pages = 0
        max_pages = 500  # предохранитель от бесконечных циклов
        last_sig: Optional[str] = None
        max_records = 200000  # ограничение общего числа записей (safety)
        max_records = 50000  # ограничение общего числа записей (safety)
        max_records = 50000  # ограничение общего числа записей (safety)
        
        while True:
            params: Dict[str, Any] = {}
            
            # Параметры пагинации
            params['$top'] = top
            params['$skip'] = skip
            
            # Фильтр, если задан
            if filter_query:
                params['$filter'] = filter_query
                
            # $select только с верхнеуровневыми полями
            sanitized = self._sanitize_select_fields(select_fields)
            if sanitized:
                params['$select'] = ','.join(sanitized)
            
            # Получаем данные (с таймаутом по умолчанию)
            result = self._make_request(f"{entity_name}", params)
            
            # Извлекаем записи
            if 'value' in result:
                data = result['value']
                if not data:  # Нет больше данных
                    break
                
                all_data.extend(data)
                
                # Ограничение по общему числу записей
                if len(all_data) >= max_records:
                    break
                
                # Если страница меньше top — это последняя страница
                if len(data) < top:
                    break
                
                # Сигнатура страницы (детектор повторяющейся страницы, если сервер игнорирует $skip)
                try:
                    head = data[:3] if isinstance(data, list) else []
                    sig = f"{len(data)}|{json.dumps(head, ensure_ascii=False, sort_keys=True)}"
                except Exception:
                    sig = f"{len(data)}"
                
                if last_sig is not None and sig == last_sig:
                    # Повторяющаяся страница — прерываем во избежание бесконечного цикла
                    break
                
                last_sig = sig
                skip += len(data)
                pages += 1
                if pages >= max_pages:
                    # Достигли защитного лимита страниц
                    break
            else:
                # Если нет 'value', значит ответ — одиночная запись
                if result:
                    all_data.append(result)
                break
                
        return all_data


def convert_1c_stock_to_dataframe(
    stock_data: List[Dict[str, Any]],
    key_to_code: Optional[Dict[str, Dict[str, str]]] = None,
    key_field_name: str = "Номенклатура_Key",
) -> List[Dict[str, Any]]:
    """
    Преобразовать данные об остатках из 1С в формат, совместимый с существующими модулями.
    
    Args:
        stock_data: Список записей об остатках из 1С
        key_to_code: Опциональная таблица соответствий Ref_Key -> {"Code","Description"} для Номенклатуры
        key_field_name: Имя поля ключа номенклатуры в записях регистра (по умолчанию Номенклатура_Key)
        
    Returns:
        Список словарей с нормализованными данными
    """
    converted_data: List[Dict[str, Any]] = []
    
    for record in stock_data:
        # Извлекаем информацию о номенклатуре (если сервер вернул навигационную структуру)
        nomenclature = record.get('Номенклатура', {}) or {}
        
        # Пытаемся получить код и наименование
        item_code = (
            nomenclature.get('Артикул')
            or nomenclature.get('Код')
            or record.get('Код')  # на случай, если поле положили плоско
        )
        item_name = nomenclature.get('Наименование') or record.get('Наименование')
        
        # Если кода нет, пробуем через Ref_Key и маппинг
        if not item_code:
            ref_key = record.get(key_field_name)
            if key_to_code and ref_key and ref_key in key_to_code:
                item_code = key_to_code[ref_key].get("Code") or item_code
                if not item_name:
                    item_name = key_to_code[ref_key].get("Description") or item_name
        
        # Получаем количество (поддержка разных наименований поля количества в разных регистрах 1С)
        qty = None
        for qf in ['Количество', 'Остаток', 'КоличествоОстаток', 'КоличествоОстатка', 'КоличествоНаСкладе', 'ОстатокКоличество', 'Quantity', 'Qty']:
            if qf in record and record.get(qf) is not None:
                try:
                    qty = float(record.get(qf))
                    break
                except Exception:
                    continue
        if qty is None:
            qty = 0.0
        
        # Добавляем запись в результат
        if item_code:
            converted_data.append({
                'code': str(item_code).strip(),
                'name': str(item_name).strip() if item_name else '',
                'qty': qty
            })
    
    return converted_data


def get_stock_from_1c_odata(
    base_url: str,
    entity_name: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None,
    filter_query: Optional[str] = None,
    select_fields: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Получить данные об остатках из 1С через OData и преобразовать в формат DataFrame.
    
    Args:
        base_url: Базовый URL OData сервиса 1С
        entity_name: Имя сущности OData (например, 'ОстаткиНоменклатуры')
        username: Имя пользователя для Basic аутентификации
        password: Пароль для Basic аутентификации
        token: Токен для Bearer аутентификации
        filter_query: Фильтр OData
        select_fields: Список полон для выборки
        
    Returns:
        Список словарей с данными об остатках
    """
    client = OData1CClient(base_url, username, password, token)
    stock_data = client.get_all_stock_data(entity_name, filter_query, select_fields)
    
    # Если у записей нет вложенной Номенклатуры, но есть Номенклатура_Key — подтянем коды каталога
    keys = [str(r.get("Номенклатура_Key")).strip() for r in stock_data if r.get("Номенклатура_Key")]
    keys = sorted({k for k in keys if k})
    key_to_code: Optional[Dict[str, Dict[str, str]]] = None
    if keys:
        try:
            key_to_code = client.get_nomenclature_codes(keys)
        except Exception:
            key_to_code = None
    
    return convert_1c_stock_to_dataframe(stock_data, key_to_code=key_to_code)