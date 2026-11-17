@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 检查依赖...
python -m pip install -q -r requirements.txt
echo [2/2] 启动服务: http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause