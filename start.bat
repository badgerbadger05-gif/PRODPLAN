@echo off
chcp 65001 >nul
title PRODPLAN

echo Запуск PRODPLAN...

REM Проверка наличия docker
docker --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Ошибка: Docker не найден.
    echo Пожалуйста, установите Docker Desktop: https://www.docker.com/products/docker-desktop
    pause
    exit /b 1
)

REM Проверка наличия docker-compose
docker-compose --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Ошибка: docker-compose не найден.
    echo Пожалуйста, установите Docker Desktop, который включает docker-compose: https://www.docker.com/products/docker-desktop
    pause
    exit /b 1
)

REM Остановка всех предыдущих сервисов
echo Остановка предыдущих запущенных сервисов...
docker-compose down 2>nul

REM Принудительное завершение процессов node.js (если фронтенд запущен вне контейнера)
echo Завершение процессов node.js...
taskkill /f /im node.exe 2>nul

REM Завершение процессов npm (если фронтенд запущен через npm run dev)
echo Завершение процессов npm...
taskkill /f /im npm.exe 2>nul

REM Запуск всех сервисов с помощью docker-compose
echo Запуск сервисов...
docker-compose up -d

if %errorlevel% equ 0 (
    echo PRODPLAN успешно запущен!
    echo Backend доступен по адресу: http://localhost:8000
    echo Frontend доступен по адресу: http://localhost:9000
    echo PostgreSQL доступен по адресу: localhost:5432
    echo.
    echo Ожидание загрузки фронтенда...
    
    REM Проверка доступности фронтенда
    set count=0
    :check_frontend
    curl -s -f http://localhost:9000 >nul 2>&1
    if %errorlevel% equ 0 (
        echo Фронтенд успешно загружен!
        echo Открываю фронтенд в браузере...
        start http://localhost:9000
    ) else (
        set /a count+=1
        if !count! gtr 30 (
            echo Не удалось дождаться загрузки фронтенда. Открываю браузер...
            start http://localhost:9000
        ) else (
            timeout /t 2 /nobreak >nul
            goto check_frontend
        )
    )
) else (
    echo Ошибка при запуске PRODPLAN.
    echo Проверьте вывод выше для получения дополнительной информации.
)

pause