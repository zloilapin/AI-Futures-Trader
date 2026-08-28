@echo off
cd /d "%~dp0"
set PYTHONPATH=%~dp0python
python scratch\test_eip712.py
pause
