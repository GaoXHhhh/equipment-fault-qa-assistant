@echo off
chcp 65001 >nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /PID %%p /F
echo 已停止 8000 端口上的服务
pause
