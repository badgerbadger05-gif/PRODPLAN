@echo off
setlocal enabledelayedexpansion
pushd "%~dp0"

rem ===========================================
rem  PRODPLAN: Инициализация схемы БД (Windows)
rem  Проект преимущественно на русском — включаем UTF‑8 для корректного ввода/вывода.
rem ===========================================
chcp 65001 >NUL
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ===========================================
rem  Использование:
rem    - двойной клик или в терминале:
rem        init_db.bat
rem    - изменить путь к БД (опционально):
rem        set DB_PATH=d:\DATA\specifications.db & init_db.bat
rem  Скрипт:
rem    1) Устанавливает зависимости из requirements.txt
rem    2) Инициализирует SQLite БД через main.py init-db
rem  Приоритет Python:
rem    - venv\Scripts\python.exe (если есть)
rem    - python из PATH
rem ===========================================

if not defined DB_PATH set "DB_PATH=data\specifications.db"

set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

echo [INIT-DB] Installing dependencies from requirements.txt ...
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :err

echo [INIT-DB] Initializing SQLite schema at "%DB_PATH%" ...
"%PY%" main.py init-db --db "%DB_PATH%"
if errorlevel 1 goto :err

echo [INIT-DB] Done.
popd
exit /b 0

:err
echo [INIT-DB] ERROR occurred. See messages above.
popd
exit /b 1