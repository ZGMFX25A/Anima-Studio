@echo off
chcp 65001 >nul
title Anima Studio
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [!] .venv not found. Create the Python environment first, see README.md.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -s "%~dp0frontend\run_frontend.py"
if errorlevel 1 pause
