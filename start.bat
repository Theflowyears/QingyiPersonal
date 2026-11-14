@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo [error] venv pythonw.exe not found
    pause
    exit /b 1
)
if not exist "config.env" (
    echo [error] config.env missing
    pause
    exit /b 1
)

netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [skip] bot already running ^(port 5000^)
    timeout /t 4 /nobreak >nul
    exit /b 0
)

start "" /B "venv\Scripts\pythonw.exe" "launcher.py"
echo [ok] started in background
echo       console: http://127.0.0.1:5000/admin/dashboard
echo       logs   : logs\
timeout /t 4 /nobreak >nul
exit /b 0
