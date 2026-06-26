#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PYTHON_EXE="$REPO_ROOT/py3_script_runner/venv/bin/python"

if [ ! -f "$PYTHON_EXE" ]; then
    echo "[ERROR] $PYTHON_EXE niet gevonden. Draai eerst install_repo.sh."
    exit 1
fi

echo "[INFO] Start Script Builder..."
echo "[INFO] Default URL: http://127.0.0.1:8765/"
echo "[INFO] Extra argumenten worden doorgestuurd, bijvoorbeeld: --port 8770 --no-browser"

"$PYTHON_EXE" "$REPO_ROOT/py3_script_runner/script_runner_app.py" "$@"
