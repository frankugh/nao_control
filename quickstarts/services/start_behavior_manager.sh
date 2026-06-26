#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PYTHON_EXE="$REPO_ROOT/py3_nao_behavior_manager/venv/bin/python"

if [ ! -f "$PYTHON_EXE" ]; then
    echo "[ERROR] $PYTHON_EXE niet gevonden. Draai eerst install_repo.sh."
    exit 1
fi

BEHAVIOR_PORT="5201"
read -r -p "Op welke port wil je de behavior manager starten? [$BEHAVIOR_PORT] " input
if [ -n "${input:-}" ]; then BEHAVIOR_PORT="$input"; fi

echo "[INFO] Start behavior manager op http://127.0.0.1:$BEHAVIOR_PORT"
"$PYTHON_EXE" "$REPO_ROOT/py3_nao_behavior_manager/py3_server.py" --host 127.0.0.1 --port "$BEHAVIOR_PORT"
