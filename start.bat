@echo off
setlocal
cd /d "%~dp0"
title Equipment Fault QA Assistant

if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul 2>nul

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if not defined PY (
  python --version >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Install Python 3.12 or 3.13 from python.org and check "Add python.exe to PATH".
    pause
    exit /b 1
  )
  echo [1/3] Creating virtual environment .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create .venv.
    pause
    exit /b 1
  )
  set "PY=.venv\Scripts\python.exe"
)

echo [2/3] Checking dependencies ...
"%PY%" -c "import uvicorn, fastapi, jieba, httpx" >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies, first run may take a few minutes ...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed.
    echo If the network is slow, run this first then retry:
    echo   set HTTPS_PROXY=http://127.0.0.1:7890
    pause
    exit /b 1
  )
)

echo [3/3] Starting server: http://127.0.0.1:8000
echo Keep this window open. Press Ctrl+C to stop.
echo.
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo Server stopped.
pause