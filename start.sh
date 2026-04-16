#!/bin/bash

# Установка кодировки UTF-8 для корректной работы с русским языком
export LC_ALL=ru_RU.UTF-8
export LANG=ru_RU.UTF-8
set -euo pipefail

# Скрипт запуска PRODPLAN

echo "Запуск PRODPLAN..."

# Проверка наличия docker
if ! command -v docker &> /dev/null
then
    echo "Ошибка: Docker не найден."
    echo "Пожалуйста, установите Docker: https://docs.docker.com/get-docker/"
    exit 1
fi

# Определяем команду Compose (предпочтительно plugin: `docker compose`)
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo "Ошибка: Docker Compose не найден (ни \`docker compose\`, ни \`docker-compose\`)."
    echo "Пожалуйста, установите Docker Compose: https://docs.docker.com/compose/install/"
    exit 1
fi

# Запуск всех сервисов с помощью docker-compose
echo "Запуск сервисов..."
if $COMPOSE_CMD up -d --build; then
    http_ok() {
        local url="$1"
        if command -v curl > /dev/null 2>&1; then
            curl -fsS "$url" > /dev/null 2>&1
        elif command -v wget > /dev/null 2>&1; then
            wget -q -O /dev/null "$url"
        else
            # Если нет ни curl, ни wget — пропускаем активную проверку.
            return 0
        fi
    }

    echo "Ожидание готовности backend..."
    for i in {1..60}; do
        if http_ok "http://localhost:8000/health"; then
            break
        fi
        sleep 2
    done

    echo "Ожидание готовности frontend..."
    for i in {1..60}; do
        if http_ok "http://localhost:9000/"; then
            break
        fi
        sleep 2
    done

    echo "PRODPLAN успешно запущен!"
    echo "Backend доступен по адресу: http://localhost:8000"
    echo "Frontend доступен по адресу: http://localhost:9000"
    echo "PostgreSQL доступен по адресу: localhost:5432"
else
    echo "Ошибка при запуске PRODPLAN."
    echo "Проверьте вывод выше для получения дополнительной информации."
    exit 1
fi
