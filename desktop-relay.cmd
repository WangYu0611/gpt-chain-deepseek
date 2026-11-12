@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if exist "%~dp0.venv\Scripts\python.exe" goto local_python
where py >nul 2>nul
if not errorlevel 1 goto python_launcher
where python >nul 2>nul
if not errorlevel 1 goto system_python

echo Python 3.10+ is required. See README.md for desktop UI dependencies. 1>&2
exit /b 1

:local_python
"%~dp0.venv\Scripts\python.exe" -u "%~dp0desktop_relay.py" %*
exit /b %errorlevel%

:python_launcher
py -3 -u "%~dp0desktop_relay.py" %*
exit /b %errorlevel%

:system_python
python -u "%~dp0desktop_relay.py" %*
exit /b %errorlevel%
