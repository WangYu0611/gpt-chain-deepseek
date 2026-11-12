@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -u -m unattended %*
) else (
    py -3 -u -m unattended %*
)
set "UNATTENDED_EXIT=%errorlevel%"
popd
exit /b %UNATTENDED_EXIT%
