#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[INFO] Start Py2 NAO base controller in nieuw venster..."
osascript -e "tell application \"Terminal\" to do script \"bash \\\"$SCRIPT_DIR/start_base_controller.sh\\\"\""

echo "[INFO] Start Py3 behavior manager in nieuw venster..."
osascript -e "tell application \"Terminal\" to do script \"bash \\\"$SCRIPT_DIR/start_behavior_manager.sh\\\"\""

echo "[INFO] Beide service-launchers zijn gestart."
