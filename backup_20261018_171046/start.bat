@echo off
rem 跨平台启动（Windows）：python run.py
cd /d "%~dp0"
where py >nul 2>nul && (py -3 run.py %* & goto :eof)
where python >nul 2>nul && (python run.py %* & goto :eof)
echo 未找到 python：请安装 Python 3.9+ 后重试。
pause
