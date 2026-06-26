#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PYTHON_EXE="$REPO_ROOT/py3_dialog_manager/venv/bin/python"

if [ ! -f "$PYTHON_EXE" ]; then
    echo "[ERROR] $PYTHON_EXE niet gevonden. Draai eerst install_repo.sh."
    exit 1
fi

echo "[INFO] Start DM Virtuele robot op http://127.0.0.1:5303/"
(sleep 2 && open "http://127.0.0.1:5303/") &
"$PYTHON_EXE" "$REPO_ROOT/py3_dialog_manager/webapp_server.py" --host 127.0.0.1 --port 5303 --preset virtuele_robot
