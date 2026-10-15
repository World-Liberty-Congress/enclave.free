#!/usr/bin/env bash
# Current SQLite/Postgres/Qdrant workflow. Legacy positional arguments fail closed.
set -euo pipefail
script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${script_root}/operations/backup.py" restore "$@"
