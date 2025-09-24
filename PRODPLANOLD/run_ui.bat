@echo off
setlocal enabledelayedexpansion
pushd "%~dp0"

rem ===========================================
rem  PRODPLAN: Запуск UI (Streamlit)
rem  Русская локаль: включаем UTF‑8 для вывода.
rem  Использование:
rem    - двойной клик или в терминале:
rem        run_ui.bat
rem    - с указанием порта:
rem        run_ui.bat 8502
rem  Скрипт:
rem    1) Устанавливает зависимости из requirements.txt
rem    2) Инициализирует БД (идемпотентно)
rem    3) Запускает Streamlit UI на http://localhost:PORT
rem ===========================================

chcp 65001 >NUL
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PORT=8501"
if not "%~1"=="" set "PORT=%~1"
set "DB_PATH=data\specifications.db"

set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

echo [UI] Checking if pip is available...
"%PY%" -m pip --version >NUL 2>&1
if errorlevel 1 (
    echo [UI] WARNING: pip is not available. Skipping dependency check.
    echo [UI] Make sure all required packages are installed manually.
) else (
    echo [UI] Checking dependencies from requirements.txt ...
    rem Проверяем, установлены ли зависимости (без forced reinstall)
    "%PY%" -m pip install --dry-run -r requirements.txt >NUL 2>&1
    if errorlevel 1 (
        echo [UI] Installing missing dependencies from requirements.txt ...
        "%PY%" -m pip install -r requirements.txt
        if errorlevel 1 goto :err
    ) else (
        echo [UI] All dependencies are already installed.
    )
)

echo [UI] Initializing SQLite schema at "%DB_PATH%" ...
"%PY%" main.py init-db --db "%DB_PATH%"
if errorlevel 1 goto :err

echo [UI] Starting Streamlit at http://localhost:%PORT% ...
rem Запускаем Streamlit напрямую (блокирующий запуск)
"%PY%" -m streamlit run src\ui.py --server.port %PORT%
if errorlevel 1 goto :err

echo [UI] Streamlit exited.
popd
pause
exit /b 0

:err
echo [UI] ERROR occurred. See messages above.
popd
pause
exit /b 1