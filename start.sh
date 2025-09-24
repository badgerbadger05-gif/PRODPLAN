#!/bin/bash

# Установка кодировки UTF-8 для корректной работы с русским языком
export LC_ALL=ru_RU.UTF-8
export LANG=ru_RU.UTF-8

# Скрипт запуска PRODPLAN

echo "Запуск PRODPLAN..."

# Проверка наличия docker
if ! command -v docker &> /dev/null
then
    echo "Ошибка: Docker не найден."
    echo "Пожалуйста, установите Docker: https://docs.docker.com/get-docker/"
    exit 1
fi

# Проверка наличия docker-compose
if ! command -v docker-compose &> /dev/null
then
    echo "Ошибка: docker-compose не найден."
    echo "Пожалуйста, установите Docker Compose: https://docs.docker.com/compose/install/"
    exit 1
fi

# Запуск всех сервисов с помощью docker-compose
echo "Запуск сервисов..."
if docker-compose up -d; then
    echo "PRODPLAN успешно запущен!"
    echo "Backend доступен по адресу: http://localhost:8000"
    echo "Frontend доступен по адресу: http://localhost:9000"
    echo "PostgreSQL доступен по адресу: localhost:5432"
else
    echo "Ошибка при запуске PRODPLAN."
    echo "Проверьте вывод выше для получения дополнительной информации."
    exit 1
fi