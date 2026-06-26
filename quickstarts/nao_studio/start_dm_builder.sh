#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PYTHON_EXE="$REPO_ROOT/py3_dialog_manager/venv/bin/python"
SERVER_SCRIPT="$REPO_ROOT/py3_dialog_manager/webapp_server.py"
AGENT_PRESETS_PATH="$REPO_ROOT/py3_dialog_manager/configs/agent_presets.json"
PRESET_PORTS_PATH="$SCRIPT_DIR/preset_ports.local.json"
DEFAULT_PRESET="virtuele_robot"
FALLBACK_PORT="5301"
NEW_PRESET_DEFAULT_PORT="8080"

if [ ! -f "$PYTHON_EXE" ]; then
    echo "[dm-start] Python venv niet gevonden: $PYTHON_EXE"
    echo "[dm-start] Draai eerst install_repo.sh."
    exit 1
fi

if [ ! -f "$SERVER_SCRIPT" ]; then
    echo "[dm-start] Server script niet gevonden: $SERVER_SCRIPT"
    exit 1
fi

# Haal startup preset IDs op uit config
PRESET_IDS=""
if [ -f "$AGENT_PRESETS_PATH" ]; then
    PRESET_IDS="$("$PYTHON_EXE" -c "
import json
try:
    with open('$AGENT_PRESETS_PATH') as f:
        data = json.load(f)
    ids = [str(p['id']) for p in data.get('presets', []) if p.get('startup_allowed')]
    print(' '.join(ids))
except Exception:
    pass
" 2>/dev/null || echo "")"
fi

get_preset_port() {
    local preset_id="$1"
    if [ -z "$preset_id" ] || [ ! -f "$PRESET_PORTS_PATH" ]; then echo ""; return; fi
    "$PYTHON_EXE" -c "
import json
try:
    with open('$PRESET_PORTS_PATH') as f:
        data = json.load(f)
    val = data.get('$preset_id', '')
    print(str(val) if val else '')
except Exception:
    pass
" 2>/dev/null || echo ""
}

save_preset_port() {
    local preset_id="$1"
    local port="$2"
    if [ -z "$preset_id" ]; then return; fi
    "$PYTHON_EXE" -c "
import json
path = '$PRESET_PORTS_PATH'
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    data = {}
data['$preset_id'] = '$port'
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null || true
}

# Preset selectie
read -r -p "Wil je een preset gebruiken? [$DEFAULT_PRESET] ([l] om lijst van opties te zien) " raw
preset="${raw:-$DEFAULT_PRESET}"

while true; do
    lower_preset="$(echo "$preset" | tr '[:upper:]' '[:lower:]')"
    if [ "$lower_preset" = "l" ]; then
        if [ -z "$PRESET_IDS" ]; then
            echo "[dm-start] Geen startup presets gevonden."
        else
            echo "[dm-start] Beschikbare presets: $PRESET_IDS"
            echo "[dm-start] Gebruik 'none' of 'geen' om zonder preset te starten."
        fi
        read -r -p "Wil je een preset gebruiken? [$DEFAULT_PRESET] " raw
        preset="${raw:-$DEFAULT_PRESET}"
        continue
    fi
    if [ "$lower_preset" = "none" ] || [ "$lower_preset" = "geen" ]; then
        preset=""
        break
    fi
    if [ -z "$PRESET_IDS" ] || echo " $PRESET_IDS " | grep -q " ${preset} "; then
        break
    fi
    echo "[dm-start] Startup preset niet gevonden. Gebruik 'l' voor de lijst."
    read -r -p "Wil je een preset gebruiken? [$DEFAULT_PRESET] " raw
    preset="${raw:-$DEFAULT_PRESET}"
done

# Port selectie
default_port="$FALLBACK_PORT"
if [ -n "$preset" ]; then
    saved_port="$(get_preset_port "$preset")"
    if [ -n "$saved_port" ]; then
        default_port="$saved_port"
    else
        default_port="$NEW_PRESET_DEFAULT_PORT"
    fi
fi

while true; do
    read -r -p "Welke port wil je gebruiken? [$default_port] " raw
    port="${raw:-$default_port}"
    if [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -gt 0 ] && [ "$port" -le 65535 ]; then
        break
    fi
    echo "[dm-start] Ongeldige poort: $port"
done

# Browser openen?
open_browser=false
while true; do
    read -r -p "Wil je de front-end automatisch openen? [N/y] " raw
    case "$(echo "${raw:-}" | tr '[:upper:]' '[:lower:]')" in
        ""|n|no|nee) open_browser=false; break ;;
        y|yes|j|ja)  open_browser=true;  break ;;
        *) echo "[dm-start] Vul y of n in." ;;
    esac
done

[ -n "$preset" ] && save_preset_port "$preset" "$port"

COMMAND_ARGS=("$SERVER_SCRIPT" --host 127.0.0.1 --port "$port")
[ -n "$preset" ] && COMMAND_ARGS+=(--preset "$preset")

echo "[dm-start] Command: \"$PYTHON_EXE\" ${COMMAND_ARGS[*]}"

[ "$open_browser" = "true" ] && (sleep 2 && open "http://127.0.0.1:$port/") &

"$PYTHON_EXE" "${COMMAND_ARGS[@]}"
