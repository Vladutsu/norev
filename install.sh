#!/usr/bin/env bash
# norev (Romanian, Local council) for Linux: one-time install, the same steps as install.cmd.
#   ./install.sh                 everything, with Speakers (diarization)
#   ./install.sh --no-speakers   without diarization
# ./norev-app.sh runs this by itself the first time.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
    PY="$(command -v python3.12 || command -v python3 || true)"
    [[ -n "$PY" ]] || { echo "[norev] Instalează Python 3.12 (python3.12 și python3.12-venv), apoi rulează din nou ./install.sh"; exit 1; }
    echo "[norev] Creez mediul .venv ..."
    "$PY" -m venv .venv
fi

echo "[norev] Instalez pachetele ..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt -r requirements-ui.txt

# Detects RAM and GPU, picks the tier, installs CUDA or CPU-only extras, downloads the models and llama.cpp
.venv/bin/python -m norev.launcher setup "$@"

.venv/bin/python -m norev.launcher new-key
echo
.venv/bin/python -m norev.launcher doctor || true
echo
echo "[norev] Instalare terminată. Pornește aplicația cu ./norev-app.sh"
