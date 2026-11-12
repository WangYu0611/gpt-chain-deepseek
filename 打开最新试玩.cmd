@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -B -m unattended.playtest
if errorlevel 1 pause
