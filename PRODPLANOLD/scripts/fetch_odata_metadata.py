#!/usr/bin/env python3
"""
Скрипт для выгрузки метаданных OData из 1С.
Сохраняет $metadata в XML файл и создает summary JSON.
"""

import argparse
import json
import sys
from pathlib import Path
from urllib import request as _urlreq, error as _urlerr
import urllib.parse as _urlparse
import base64

def fetch_metadata(url: str, username: str = None, password: str = None, timeout: float = 30.0):
    """
    Выгружает $metadata из OData сервиса.
    Возвращает (xml_content, error_message)
    """
    metadata_url = url.rstrip('/') + '/$metadata'

    # Создаем запрос
    req = _urlreq.Request(metadata_url)
    req.add_header('Accept', 'application/xml')

    # Добавляем аутентификацию, если указана
    if username and password:
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        req.add_header('Authorization', f'Basic {credentials}')

    try:
        with _urlreq.urlopen(req, timeout=timeout) as response:
            xml_content = response.read().decode('utf-8', errors='ignore')
            return xml_content, None
    except Exception as e:
        return None, str(e)

def create_summary(xml_content: str) -> dict:
    """
    Создает summary JSON из XML метаданных.
    Простая версия - извлекает основные сущности.
    """
    summary = {
        "metadata_url": "extracted from XML",
        "entities": [],
        "entity_sets": [],
        "functions": [],
        "actions": []
    }

    try:
        # Простой парсинг XML для извлечения EntitySet
        lines = xml_content.split('\n')
        for line in lines:
            line = line.strip()
            if 'EntitySet Name=' in line and 'EntityType=' in line:
                # Извлекаем имя EntitySet
                start = line.find('Name="') + 6
                end = line.find('"', start)
                if start > 5 and end > start:
                    entity_set = line[start:end]
                    summary["entity_sets"].append(entity_set)
            elif '<EntityType Name=' in line:
                # Извлекаем имя EntityType
                start = line.find('Name="') + 6
                end = line.find('"', start)
                if start > 5 and end > start:
                    entity = line[start:end]
                    summary["entities"].append(entity)
    except Exception:
        pass

    return summary

def main():
    parser = argparse.ArgumentParser(description='Выгрузка метаданных OData из 1С')
    parser.add_argument('--url', required=True, help='URL OData сервиса (без $metadata)')
    parser.add_argument('--out', required=True, help='Путь к выходному XML файлу')
    parser.add_argument('--summary-out', required=True, help='Путь к выходному summary JSON файлу')
    parser.add_argument('--username', help='Имя пользователя для Basic Auth')
    parser.add_argument('--password', help='Пароль для Basic Auth')
    parser.add_argument('--timeout', type=float, default=30.0, help='Таймаут запроса в секундах')

    args = parser.parse_args()

    print(f"Выгрузка метаданных из: {args.url}")

    # Выгружаем метаданные
    xml_content, error = fetch_metadata(args.url, args.username, args.password, args.timeout)

    if error:
        print(f"Ошибка: {error}", file=sys.stderr)
        sys.exit(1)

    if not xml_content:
        print("Ошибка: пустой ответ от сервера", file=sys.stderr)
        sys.exit(1)

    # Сохраняем XML
    try:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(xml_content)
        print(f"XML сохранен: {args.out}")
    except Exception as e:
        print(f"Ошибка сохранения XML: {e}", file=sys.stderr)
        sys.exit(1)

    # Создаем и сохраняем summary
    try:
        summary = create_summary(xml_content)
        with open(args.summary_out, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"Summary JSON сохранен: {args.summary_out}")
        print(f"Найдено EntitySet: {len(summary.get('entity_sets', []))}")
        print(f"Найдено EntityType: {len(summary.get('entities', []))}")
    except Exception as e:
        print(f"Ошибка создания summary: {e}", file=sys.stderr)
        sys.exit(1)

    print("Выгрузка метаданных завершена успешно")

if __name__ == '__main__':
    main()