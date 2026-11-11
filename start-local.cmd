@echo off
setlocal
cd /d "%~dp0"
set "NOREV_HOME=%~dp0..\runtime"
set "NOREV_EXPORT_DIR=%~dp0..\runtime\exports"
set "NOREV_DOMAIN=local_council"
set "NOREV_DIARIZER=sherpa"
set "NOREV_TRANSLATE=0"
set "LLM_PARALLEL=1"
set "LLAMACPP_CTX=8192"
set "LLAMACPP_GPU_LAYERS=99"
set "WHISPER_MODEL=large-v3-turbo"
set "WHISPER_DEVICE=cuda"
set "PYTHONUTF8=1"
"%~dp0.venv\Scripts\python.exe" -m norev.launcher %*
if errorlevel 1 pause
