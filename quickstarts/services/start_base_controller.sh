#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PYTHON_EXE="$REPO_ROOT/py2_nao_base_controller/venv/bin/python"

if [ ! -f "$PYTHON_EXE" ]; then
    echo "[ERROR] $PYTHON_EXE niet gevonden. Draai eerst install_repo.sh."
    exit 1
fi

BASE_PORT="5101"
read -r -p "Op welke port wil je de base controller starten? [$BASE_PORT] " input
if [ -n "${input:-}" ]; then BASE_PORT="$input"; fi

echo "[INFO] Start base controller op http://127.0.0.1:$BASE_PORT"
"$PYTHON_EXE" "$REPO_ROOT/py2_nao_base_controller/nao_api.py" --host 127.0.0.1 --port "$BASE_PORT"
