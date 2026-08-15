@echo off
cd /d "%~dp0"
set PYTHONPATH=%~dp0python
python python\main.py
pause
