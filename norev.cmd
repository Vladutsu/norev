@echo off
rem norev (Romana, Corporate) for Windows. Without arguments: check this machine, then open the app.
rem The first time it installs everything (install.cmd). Other commands:
rem   norev.cmd doctor ^| setup ^| new-key ^| setup-models ^| process ^<file^>
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [norev] Prima pornire: instalez norev pe acest calculator ...
  call install.cmd || exit /b 1
)
".venv\Scripts\python.exe" -m norev.launcher %*
exit /b %ERRORLEVEL%
