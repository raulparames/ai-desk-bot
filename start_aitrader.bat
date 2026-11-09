@echo off
REM Starts the local AI-Trader paper-trading server on http://localhost:8000
cd /d "%~dp0..\AI-Trader"
.venv\Scripts\python.exe service\server\main.py
pause
