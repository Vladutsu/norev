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

step "python compiles"
"$PY" -m compileall -q LLM norev

step "every module imports"
"$PY" - <<'PY'
import importlib, pkgutil, sys
import LLM, norev

failed = []
for pkg in (LLM, norev):
    for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        try:
            importlib.import_module(mod.name)
        except Exception as exc:
            failed.append(f"  {mod.name}: {exc!r}")
if failed:
    print("import failed:\n" + "\n".join(failed))
    sys.exit(1)
PY

if compgen -G "tests/test_*.py" >/dev/null; then
    step "unit tests"
    "$PY" -m unittest discover -s tests
fi

echo "OK"
