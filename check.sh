#!/usr/bin/env bash
# Checks that must pass before pushing to main. CI runs exactly this script.
# Usage: ./check.sh        (uses .venv if present, otherwise python3)
# No GPU, no models, no llama-server needed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    if [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi
fi

step() { echo "==> $*"; }

step "bash syntax"
for f in *.sh; do bash -n "$f"; done

"$PY" scripts/check.py
