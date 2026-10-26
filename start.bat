@echo off
rem AI 驱动的 HYSYS 反应器智能建模 —— Web 演示一键启动
rem 双击后：启动 FastAPI 服务（端口 8000）并自动打开浏览器
cd /d %~dp0
start "" cmd /c "timeout /t 2 >nul & start http://localhost:8000"
python app.py
pause
