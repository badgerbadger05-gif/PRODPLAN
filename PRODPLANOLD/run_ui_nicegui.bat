@echo off
chcp 65001 >nul
setlocal ENABLEDELAYEDEXPANSION

REM ========= Config =========
set PORT=%1
if "%PORT%"=="" set PORT=8080
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
REM ==========================

echo.
echo [run_ui_nicegui] Ensuring port %PORT% is free...

REM Kill any process listening on the target port (Windows)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
  echo [run_ui_nicegui] Killing PID %%a using port %PORT% ...
  taskkill /F /PID %%a >nul 2>&1
)

REM Small delay to let the OS release the port
timeout /T 1 >nul

REM Optionally install dependencies (uncomment if needed)
REM python -m pip install --upgrade pip
REM python -m pip install -r requirements.txt

echo [run_ui_nicegui] Starting NiceGUI dev server on http://localhost:%PORT% ...
REM NiceGUI will honor ui.run(port=PORT) in code. If needed, pass via env:
set NICEGUI_PORT=%PORT%

python -m src.ui_nicegui.app