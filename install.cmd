@echo off
rem norev for Windows: one-time install (#48). Creates .venv and installs the Python packages.
rem   install.cmd             app + transcription
rem   install.cmd --speakers  also diarization (Speakers), a much larger download
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto packages
echo [norev] Creez mediul .venv ...
py -3.12 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo [norev] Nu am gasit Python 3.12. Instaleaza-l de pe python.org ^(bifeaza "Add python.exe to PATH"^), apoi ruleaza din nou install.cmd.
  exit /b 1
)

:packages
echo [norev] Instalez pachetele ...
".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-ui.txt || exit /b 1
if /i "%~1"=="--speakers" (
  ".venv\Scripts\python.exe" -m pip install -r requirements-diarization.txt || exit /b 1
)

".venv\Scripts\python.exe" -m norev.launcher new-key
echo.
".venv\Scripts\python.exe" -m norev.launcher doctor
echo.
echo [norev] Instalare terminata. Porneste aplicatia cu norev.cmd
exit /b 0
