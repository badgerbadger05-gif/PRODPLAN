@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
title PRODPLAN — Rebuild

rem Перейти в директорию скрипта (корень репозитория)
pushd "%~dp0"

echo =========================================================
echo   Пересборка и перезапуск контейнеров PRODPLAN
echo   Опции:
echo     --use-cache   - сборка с кешом (по умолчанию без кеша)
echo     --no-cache    - принудительно без кеша (по умолчанию)
echo     --pull        - подтянуть свежие базовые образы (по умолчанию включено)
echo =========================================================
echo(

rem Проверка docker
docker --version >nul 2>&1
if %errorlevel% neq 0 (
  echo [ERROR] Docker не найден. Установите Docker Desktop: https://www.docker.com/products/docker-desktop
  goto :error
)

rem Определение доступной команды Docker Compose (v2 plugin или docker-compose)
set "COMPOSE_CMD="
docker compose version >nul 2>&1
if %errorlevel% equ 0 (
  set "COMPOSE_CMD=docker compose"
) else (
  docker-compose --version >nul 2>&1
  if %errorlevel% equ 0 (
    set "COMPOSE_CMD=docker-compose"
  ) else (
    echo [ERROR] Docker Compose не найден. Установите Docker Desktop, включая Compose v2, или docker-compose.
    goto :error
  )
)
echo [INFO] Использую Compose: %COMPOSE_CMD%

rem Разбор аргументов
set "USE_CACHE=0"
set "WITH_PULL=1"

for %%A in (%*) do (
  if /I "%%~A"=="--use-cache" set "USE_CACHE=1"
  if /I "%%~A"=="--no-cache"  set "USE_CACHE=0"
  if /I "%%~A"=="--pull"      set "WITH_PULL=1"
)

set "BUILD_ARGS="
if %WITH_PULL%==1 (
  set "BUILD_ARGS=--pull"
)

if %USE_CACHE%==0 (
  set "BUILD_ARGS=%BUILD_ARGS% --no-cache"
)

echo [STEP] Остановка предыдущих контейнеров...
%COMPOSE_CMD% down --remove-orphans
if %errorlevel% neq 0 (
  echo [WARN] Ошибка при остановке предыдущих контейнеров, продолжаем.
)

echo [STEP] Пересборка образов: %COMPOSE_CMD% build %BUILD_ARGS%
%COMPOSE_CMD% build %BUILD_ARGS%
if %errorlevel% neq 0 (
  echo [ERROR] Ошибка пересборки образов.
  goto :error
)

echo [STEP] Запуск контейнеров...
%COMPOSE_CMD% up -d
if %errorlevel% neq 0 (
  echo [ERROR] Ошибка запуска контейнеров.
  goto :error
)

echo(
echo Backend:  http://localhost:8000
echo Frontend: http://localhost:9000
echo DB:       localhost:5432
echo(

rem Ожидание фронтенда (до ~60 секунд)
set "count=0"
:wait_frontend
rem Проверка доступности через PowerShell (без редиректов ввода/вывода)
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:9000'; if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { exit 0 } else { exit 1 } } catch { exit 1 }"
if %errorlevel% equ 0 (
  echo [OK] Фронтенд доступен. Открываю в браузере...
  start http://localhost:9000
  goto :done
) else (
  set /a count+=1
  if !count! gtr 30 (
    echo [WARN] Не дождался фронтенда. Открою браузер для проверки.
    start http://localhost:9000
    goto :done
  ) else (
    timeout /t 2 /nobreak >nul
    goto :wait_frontend
  )
)

:done
echo(
echo [DONE] Пересборка и перезапуск выполнены.
popd
endlocal
exit /b 0

:error
echo(
echo [FAIL] Операция завершилась с ошибкой. Проверьте вывод выше.
popd
endlocal
exit /b 1