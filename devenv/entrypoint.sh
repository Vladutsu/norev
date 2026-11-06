#!/usr/bin/env bash
# Every container run first checks the dev data in /data against data.manifest (docs/ARCHITECTURE.md)
set -euo pipefail
python scripts/data.py verify
exec "$@"
