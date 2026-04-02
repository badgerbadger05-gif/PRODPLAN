# Инструкция по развертыванию и обновлению prodplan

## Информация о сервере

- **Адрес:** `mtzdock.lan` (10.36.0.12)
- **Пользователь:** `barsukov`
- **Пароль:** `Chai3rae`
- **Путь к проекту:** `/opt/prodplan`
- **Веб-интерфейс:** http://mtzdock.lan:9000 или http://10.36.0.12:9000

## Подключение к серверу

### Windows (PowerShell)
```powershell
ssh barsukov@mtzdock.lan
# Введи пароль: Chai3rae
```

### При первом подключении
```
Are you sure you want to continue connecting (yes/no/[fingerprint])? yes
barsukov@mtzdock.lan's password: Chai3rae
```

## Обновление проекта

### 1. Подключение и переход в проект
```bash
ssh barsukov@mtzdock.lan
cd /opt/prodplan
```

### 2. Остановка текущих контейнеров
```bash
# Останови все сервисы
docker compose down

# Для полной очистки (удалит образы):
docker compose down --rmi local
```

### 3. Обновление кода
```bash
# Проверь текущее состояние
git status
git branch

# Получи последние изменения
git pull origin main

# Если есть локальные изменения
git stash          # сохрани изменения
git pull           # обновись
git stash pop      # верни изменения (если нужно)
```

### 4. Запуск обновленной версии
```bash
# Собери новые образы
docker compose build

# Запусти все сервисы
docker compose up -d

# Примени миграции БД (обязательно после обновления кода)
docker compose exec backend alembic upgrade head

# Проверь статус
docker compose ps
```

## Проверка работоспособности

### Проверка контейнеров
```bash
# Статус всех сервисов проекта
docker compose ps

# Все запущенные контейнеры в системе
docker ps

# Логи всех сервисов
docker compose logs

# Логи конкретного сервиса
docker compose logs frontend
docker compose logs backend
docker compose logs db
```

### Проверка доступности
```bash
# Frontend (веб-интерфейс)
curl -I http://localhost:9000

# Backend API
curl -I http://localhost:8000

# Проверь открытые порты
netstat -tuln | grep -E ':9000|:8000|:5432'
```

### Ожидаемый результат
```bash
$ docker compose ps
NAME                  IMAGE               COMMAND                  SERVICE    STATUS          PORTS
prodplan-backend-1    prodplan-backend    "uvicorn app.main:ap…"   backend    Up XX seconds   0.0.0.0:8000->8000/tcp
prodplan-db-1         postgres:15         "docker-entrypoint.s…"   db         Up XX seconds   0.0.0.0:5432->5432/tcp
prodplan-frontend-1   prodplan-frontend   "/docker-entrypoint.…"   frontend   Up XX seconds   0.0.0.0:9000->80/tcp
```

## Диагностика проблем

### Если контейнеры не запускаются
```bash
# Логи с ошибками
docker compose logs

# Пересборка без кеша
docker compose build --no-cache

# Принудительный пересброс
docker compose down --rmi all -v
docker compose build
docker compose up -d
```

### Если frontend не может найти backend
**Ошибка:** `host not found in upstream "backend"`

**Решение:** Добавить в docker-compose.yml в секцию frontend:
```yaml
frontend:
  depends_on:
    - backend
```

### Проверка ресурсов сервера
```bash
# Место на диске
df -h

# Использование CPU и памяти
top
# (нажми 'q' для выхода)

# Процессы Docker
ps aux | grep docker
```

### Проблемы с сетевым подключением
```bash
# Проверка доступности сервера
ping mtzdock.lan
ping 10.36.0.12

# Проверка SSH порта
telnet mtzdock.lan 22
```

## Быстрые команды

### Полный перезапуск
```bash
cd /opt/prodplan
docker compose down
git pull
docker compose build
docker compose up -d
docker compose exec backend alembic upgrade head
docker compose ps
```

### Если ошибка `column items.category_id does not exist`
```bash
cd /opt/prodplan
docker compose up -d
docker compose exec backend alembic upgrade head
docker compose restart backend
docker compose logs -f backend
```

### Только перезапуск без обновления
```bash
cd /opt/prodplan
docker compose restart
docker compose ps
```

### Просмотр логов в реальном времени
```bash
cd /opt/prodplan
docker compose logs -f
# Ctrl+C для выхода
```

## Архитектура проекта

**Сервисы:**
- **db** - PostgreSQL база данных (порт 5432)
- **backend** - FastAPI/Uvicorn сервер (порт 8000)  
- **frontend** - Nginx веб-сервер (порт 9000)

**Volumes:**
- `postgres_data` - данные базы PostgreSQL
- `./config` - конфигурационные файлы
- `./output` - выходные файлы
- `./frontend/config` - конфигурация frontend

**Зависимости запуска:**
1. `db` (база данных)
2. `backend` (зависит от db)
3. `frontend` (зависит от backend)

## Работа с VS Code Remote SSH

### Подключение
1. Открой VS Code
2. Ctrl+Shift+P → "Remote-SSH: Connect to Host"
3. Выбери `barsukov@mtzdock.lan` 
4. Введи пароль `Chai3rae`

### Полезные команды в VS Code Terminal
```bash
# Быстрая диагностика
hostname && docker compose -f /opt/prodplan/docker-compose.yml ps && docker ps

# Логи с автообновлением
docker compose -f /opt/prodplan/docker-compose.yml logs -f
```

## Troubleshooting

### VS Code команды зависают
**Причина:** Контейнеры не запущены или Docker API не отвечает

**Решение:**
1. Подключись через обычный SSH в PowerShell
2. Проверь статус контейнеров: `docker compose ps`
3. Запусти контейнеры если они остановлены
4. Повтори команду в VS Code

### Веб-интерфейс недоступен
**Проверь:**
1. Контейнеры запущены: `docker compose ps`
2. Порт 9000 открыт: `netstat -tuln | grep :9000`
3. Frontend логи: `docker compose logs frontend`
4. Доступность с сервера: `curl -I http://localhost:9000`

### Ошибки базы данных
```bash
# Проверь статус PostgreSQL
docker compose logs db

# Подключение к базе
docker compose exec db psql -U prodplan -d prodplan
```

---

**Примечание:** После каждого изменения кода обязательно выполняй полное обновление с пересборкой образов (`docker compose build`) для применения изменений.
