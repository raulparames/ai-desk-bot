@echo off
REM Runs the AI Desk Bot 24/7 (paper only). Start start_aitrader.bat first.
cd /d "%~dp0"
.venv\Scripts\python.exe -m deskbot run --loop
pause
