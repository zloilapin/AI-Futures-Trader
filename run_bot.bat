@echo off
cd /d "%~dp0"
set PYTHONPATH=%~dp0python
python python\main.py > scratch\bot_output.log 2>&1
pause
