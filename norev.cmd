@echo off
rem norev for Windows (#48). Without arguments: check this machine, then open the app.
rem Other commands: norev.cmd doctor ^| new-key ^| setup-models ^| process ^<file^> [language]
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [norev] norev nu e instalat inca. Ruleaza o data install.cmd.
  exit /b 1
)
".venv\Scripts\python.exe" -m norev.launcher %*
exit /b %ERRORLEVEL%
