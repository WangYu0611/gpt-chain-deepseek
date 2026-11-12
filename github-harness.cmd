@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0github-harness-settings.pyw"
    exit /b 0
)
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "%~dp0github-harness-settings.pyw"
    exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0github-harness-settings.pyw"
    exit /b 0
)
echo Python 3.12 with Tkinter is required. See github_harness_lab\README.md.
pause
exit /b 1
