# PRODPLAN: Настройка и администрирование

**Версия:** 1.6  
**Дата:** 2025-09-16

## 🔧 Настройка окружения

### Системные требования
- **OS**: Windows 10+, Linux, macOS
- **Python**: 3.8+
- **RAM**: 4+ ГБ (рекомендуется 8 ГБ)
- **Disk**: 1 ГБ свободного места
- **Network**: Доступ к серверу 1С (для OData синхронизации)

### Установка Python зависимостей
```bash
# Создание виртуального окружения (рекомендуется)
python -m venv prodplan-env
source prodplan-env/bin/activate  # Linux/Mac
prodplan-env\Scripts\activate     # Windows

# Установка пакетов
pip install -r requirements.txt
```

### Конфигурация кодировок (обязательно для Windows)

#### В batch файлах:
```batch
@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
```

#### В PowerShell:
```powershell
$env:PYTHONUTF8="1"
$env:PYTHONIOENCODING="utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

---

## 🗄️ Настройка базы данных

### SQLite оптимизация
**Файл конфигурации** (`src/database.py`):
```python
# Рекомендуемые PRAGMA для производительности
PRAGMA foreign_keys = ON;          # Проверка внешних ключей
PRAGMA journal_mode = WAL;         # Write-Ahead Logging  
PRAGMA synchronous = NORMAL;       # Баланс производительности/надежности
PRAGMA cache_size = -64000;        # 64MB кэш (отрицательное значение = килобайты)
PRAGMA temp_store = MEMORY;        # Временные таблицы в памяти
```

### Бэкап базы данных
```bash
# Создание резервной копии
cp data/specifications.db data/backup_$(date +%Y%m%d_%H%M%S).db

# Автоматический бэкап (cron/задачи Windows)
0 2 * * * /path/to/backup_script.sh
```

### Восстановление из бэкапа
```bash
# Остановить все процессы PRODPLAN
# Заменить основную БД
cp data/backup_20250916_020000.db data/specifications.db
# Перезапустить систему
```

---

## 🔐 Интеграция с 1С

### Настройка OData API в 1С

#### Включение OData сервиса:
1. **Конфигуратор 1С** → Администрирование → Интернет-сервисы
2. **Создать HTTP-сервис** типа "OData" 
3. **URL**: `/odata/standard.odata`
4. **Аутентификация**: Basic или OS Authentication

#### Публикация сущностей:
```
✅ Справочник.Номенклатура
✅ РегистрНакопления.ЗапасыНаСкладах  
✅ Справочник.Спецификации
✅ Документ.ЗаказНаПроизводство
✅ Документ.ЗаказПоставщику
```

### Тестирование OData подключения
```bash
# Проверка доступности API
curl -u "username:password" \
  "http://srv-1c:8080/base/odata/standard.odata/\$metadata"

# Тест получения остатков
curl -u "username:password" \
  "http://srv-1c:8080/base/odata/standard.odata/AccumulationRegister_ЗапасыНаСкладах?\$top=5"
```

### Маппинг полей 1С ↔ PRODPLAN

#### Номенклатура:
| 1С поле | PRODPLAN поле | Описание |
|---------|---------------|----------|
| `Ref_Key` | `item_id` | Уникальный ID |
| `Code` | `item_code` | Код номенклатуры |
| `Description` | `item_name` | Наименование |
| `Артикул` | `item_article` | Артикул изделия |

#### Остатки:
| 1С поле | PRODPLAN поле | Описание |
|---------|---------------|----------|
| `Номенклатура_Key` | `item_id` | Ссылка на номенклатуру |
| `КоличествоОстаток` | `stock_qty` | Количество остатка |

---

## 🚀 Развертывание в продакшене

### Вариант 1: Локальная машина (Single User)
```bash
# Создание службы Windows (через NSSM)
nssm install PRODPLAN-UI
nssm set PRODPLAN-UI Application "C:\Python39\python.exe"
nssm set PRODPLAN-UI AppParameters "-m streamlit run src\ui.py --server.port 8501"
nssm set PRODPLAN-UI AppDirectory "C:\PRODPLAN"
nssm start PRODPLAN-UI
```

### Вариант 2: Docker контейнер
```dockerfile
FROM python:3.9-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
EXPOSE 8501

CMD ["streamlit", "run", "src/ui.py", "--server.address", "0.0.0.0"]
```

```bash
# Сборка и запуск
docker build -t prodplan .
docker run -d -p 8501:8501 -v $(pwd)/data:/app/data prodplan
```

### Вариант 3: LAN сервер (Multi User)
```bash
# Установка на сервере
git clone https://github.com/company/prodplan /opt/prodplan
cd /opt/prodplan

# Настройка systemd сервиса
sudo tee /etc/systemd/system/prodplan.service << EOF
[Unit]
Description=PRODPLAN Production Planning System
After=network.target

[Service]
Type=simple
User=prodplan
WorkingDirectory=/opt/prodplan
ExecStart=/usr/bin/python3 -m streamlit run src/ui.py --server.address 0.0.0.0 --server.port 8501
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable prodplan
sudo systemctl start prodplan
```

---

## 🔍 Мониторинг и логирование

### Настройка логирования
**Файл** `src/logging_config.py`:
```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/prodplan.log'),
        logging.StreamHandler()
    ]
)
```

### Мониторинг производительности
```bash
# Размер базы данных
du -h data/specifications.db

# Количество записей в таблицах
sqlite3 data/specifications.db "
  SELECT 'items: ' || COUNT(*) FROM items
  UNION ALL
  SELECT 'bom: ' || COUNT(*) FROM bom  
  UNION ALL
  SELECT 'plan_entries: ' || COUNT(*) FROM production_plan_entries
"
```

### Проверка целостности БД
```bash
# SQLite проверка
sqlite3 data/specifications.db "PRAGMA integrity_check;"

# Проверка внешних ключей
sqlite3 data/specifications.db "PRAGMA foreign_key_check;"
```

---

## ⚡ Ollama LLM интеграция (Опционально)

### Установка Ollama
```bash
# Windows (через установщик)
# Скачать с https://ollama.com/download

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Запуск модели для эмбеддингов
ollama pull nomic-embed-text
```

### Проверка API
```bash
# Тест эмбеддингов
curl http://localhost:11434/api/embeddings \
  -d '{
    "model": "nomic-embed-text",
    "prompt": "Планирование производства"
  }'
```

### Интеграция в PRODPLAN
**Добавить в** `requirements.txt`:
```
openai>=1.0.0  # Для совместимости с Ollama API
```

**Конфигурация** `src/llm_config.py`:
```python
import openai

# Настройка клиента для локальной Ollama
client = openai.OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama"  # Может быть любой строкой
)
```

---

## 🛡️ Безопасность и бэкапы

### Регулярные задачи обслуживания

#### Еженедельный скрипт (`maintenance.sh`):
```bash
#!/bin/bash
# Бэкап БД
cp data/specifications.db "backup/db_$(date +%Y%m%d).db"

# Очистка старых логов (>30 дней)
find logs/ -name "*.log" -mtime +30 -delete

# Очистка истории остатков (>30 дней)
sqlite3 data/specifications.db "
  DELETE FROM stock_history 
  WHERE recorded_at < datetime('now', '-30 days')
"

# Вакуумирование БД для освобождения места
sqlite3 data/specifications.db "VACUUM;"
```

### Контроль доступа
```bash
# Права доступа к файлам (Linux)
chmod 600 data/specifications.db  # Только владелец
chmod 644 config/*.yaml           # Чтение для группы
chmod 755 scripts/*.sh            # Исполнение скриптов
```

### Мониторинг дискового пространства
```bash
# Проверка места на диске
df -h /opt/prodplan

# Размер папок проекта  
du -sh data/ logs/ output/ backup/
```