@echo off
rem norev (Romana, Consiliu local) for Windows: one-time install. Everything is automatic:
rem   Python packages, the hardware tier of this laptop, the models and llama.cpp for that tier.
rem   install.cmd                 everything, with Speakers (diarization)
rem   install.cmd --no-speakers   without diarization (smaller download, Transcript without Speakers)
rem norev.cmd runs this by itself the first time, so double-clicking norev.cmd is enough.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto packages
echo [norev] Creez mediul .venv ...
py -3.12 -m venv .venv 2>nul || python -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto packages
rem No Python 3.12: install it for this user with winget (part of Windows 10/11), then try again
where winget >nul 2>nul && (
  echo [norev] Instalez Python 3.12 ...
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo [norev] Nu am gasit Python 3.12. Instaleaza-l de pe python.org ^(bifeaza "Add python.exe to PATH"^), apoi ruleaza din nou install.cmd.
  exit /b 1
)

:packages
echo [norev] Instalez pachetele ...
".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-ui.txt || exit /b 1

rem Detects RAM and GPU, picks the tier, installs CUDA or CPU-only extras, downloads the models and llama.cpp
".venv\Scripts\python.exe" -m norev.launcher setup %* || exit /b 1

".venv\Scripts\python.exe" -m norev.launcher new-key
echo.
".venv\Scripts\python.exe" -m norev.launcher doctor
echo.
echo [norev] Instalare terminata. Porneste aplicatia cu norev.cmd
exit /b 0
