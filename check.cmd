@echo off
rem Windows equivalent of check.sh: compile, import every module, unit tests (no GPU, models or network).
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" scripts\check.py
exit /b %ERRORLEVEL%
