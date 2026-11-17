@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "data" mkdir data
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> "data\server.log" 2>&1
