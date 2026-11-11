#!/usr/bin/env bash
# norev (Romanian, Local council) for Linux: the same as norev.cmd. Installs everything the first time.
#   ./norev-app.sh [doctor | setup | new-key | setup-models | process <file>]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    echo "[norev] Prima pornire: instalez norev pe acest calculator ..."
    ./install.sh
fi
exec .venv/bin/python -m norev.launcher "$@"
