# PRODPLAN: Быстрый старт

**Версия:** 1.6  
**Дата:** 2025-09-16

## 🚀 Что такое PRODPLAN

PRODPLAN — система планирования производства на Python с SQLite, автоматизирующая:
- **Управление спецификациями изделий (BOM)** из Excel
- **Планирование производства на 30 дней** с веб-интерфейсом  
- **Расчет заказов** с цветовой индикацией статусов
- **Синхронизацию остатков** из 1С через Excel/OData API
- **Историю остатков** за 30 дней для анализа трендов

## ⚡ Быстрая установка (5 минут)

### Шаг 1: Подготовка окружения
```bash
# Клонируем проект
git clone https://github.com/your-repo/prodplan
cd prodplan

# Устанавливаем зависимости
pip install -r requirements.txt
```

### Шаг 2: Инициализация базы данных
```bash
# Windows
init_db.bat

# Linux/Mac
python main.py init-db
```

### Шаг 3: Загрузка данных
```bash
# Синхронизация остатков из 1С (OData)
python main.py sync-stock-odata --url http://your-1c-server/odata --entity AccumulationRegister_ЗапасыНаСкладах

# Альтернативно: из Excel файлов
python main.py sync-stock --dir ostatki
```

### Шаг 4: Запуск веб-интерфейса
```bash
# Windows
run_ui.bat

# Linux/Mac
npm run dev
```

Откройте браузер: **http://localhost:9000**

## 🎯 Первое использование

### В веб-интерфейсе:
1. **Страница "План производства"** — редактируйте дневные планы, они автоматически сохраняются
2. **Кнопка "Обновить остатки"** — синхронизирует данные из 1С
3. **Экспорт CSV** — для работы в Excel

### Из командной строки:
```bash
# Генерация плана производства (Excel)
python main.py generate-plan --out output/production_plan.xlsx --days 30

# Расчет заказов
python main.py calculate-orders --output output
```

## 📋 Что дальше?

- **Архитектура системы**: [02-architecture.md](02-architecture.md)
- **Справочник команд**: [03-api-reference.md](03-api-reference.md)  
- **Настройка и деплой**: [04-admin-guide.md](04-admin-guide.md)
- **План развития**: [05-roadmap.md](05-roadmap.md)

## 🆘 Быстрая помощь

### Проблемы с кодировкой (Windows):
```bash
# В batch файлах добавить:
chcp 65001
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
```

### Ollama не работает:
```bash
# Проверить статус API
curl http://localhost:11434/api/tags

# Если порт занят - Ollama уже запущена как служба
```

### База данных пуста:
```bash
# Проверить наличие файла
ls data/specifications.db

# Переинициализировать
python main.py init-db
```