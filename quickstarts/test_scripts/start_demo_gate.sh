#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PYTHON_EXE="$REPO_ROOT/py3_dialog_manager/venv/bin/python"
DEMO_GATE_SCRIPT="$REPO_ROOT/py3_dialog_manager/scripts/run_demo_gate.py"
AGENT_PRESETS_PATH="$REPO_ROOT/py3_dialog_manager/configs/agent_presets.json"
SUMMARY_PRESETS_PATH="$REPO_ROOT/py3_dialog_manager/configs/summary_presets.json"

USE_DEFAULTS=false
for arg in "$@"; do
    [ "$arg" = "--use-defaults" ] || [ "$arg" = "-UseDefaults" ] && USE_DEFAULTS=true && break
done

if [ ! -f "$PYTHON_EXE" ]; then
    echo "[demo-gate-start] Python venv niet gevonden: $PYTHON_EXE"
    echo "[demo-gate-start] Draai eerst install_repo.sh."
    exit 1
fi

if [ ! -f "$DEMO_GATE_SCRIPT" ]; then
    echo "[demo-gate-start] Demo gate script niet gevonden: $DEMO_GATE_SCRIPT"
    exit 1
fi

# Laad preset IDs via Python
load_preset_ids() {
    local path="$1"
    local filter="${2:-}"
    if [ ! -f "$path" ]; then echo ""; return; fi
    "$PYTHON_EXE" -c "
import json
try:
    with open('$path') as f:
        data = json.load(f)
    presets = data.get('presets', [])
    if '$filter' == 'startup':
        ids = [str(p['id']) for p in presets if p.get('startup_allowed') and p.get('id')]
    else:
        ids = [str(p['id']) for p in presets if p.get('id')]
    print(' '.join(ids))
except Exception:
    pass
" 2>/dev/null || echo ""
}

STARTUP_PRESET_IDS="$(load_preset_ids "$AGENT_PRESETS_PATH" "startup")"
SUMMARY_PRESET_IDS="$(load_preset_ids "$SUMMARY_PRESETS_PATH" "")"

resolve_repo_path() {
    local value="$1"
    if [ -z "$value" ]; then echo ""; return; fi
    if [[ "$value" = /* ]]; then
        echo "$value"
    else
        echo "$REPO_ROOT/$value"
    fi
}

read_default_value() {
    local prompt="$1"
    local default="${2:-}"
    if [ "$USE_DEFAULTS" = "true" ]; then
        if [ -z "$default" ]; then
            echo "[demo-gate-start] $prompt -> <leeg>" >&2
        else
            echo "[demo-gate-start] $prompt -> $default" >&2
        fi
        echo "$default"
        return
    fi
    if [ -z "$default" ]; then
        read -r -p "$prompt: " raw
    else
        read -r -p "$prompt [$default]: " raw
    fi
    echo "${raw:-$default}"
}

read_yn_value() {
    local prompt="$1"
    local default="${2:-n}"
    while true; do
        local raw
        raw="$(read_default_value "$prompt (y/n)" "$default")"
        case "$(echo "$raw" | tr '[:upper:]' '[:lower:]')" in
            y|yes|j|ja) echo "true";  return ;;
            n|no|nee)   echo "false"; return ;;
            *) echo "[demo-gate-start] Vul y of n in." >&2 ;;
        esac
    done
}

read_choice_value() {
    local prompt="$1"
    local default="$2"
    shift 2
    local choices=("$@")
    while true; do
        local raw
        raw="$(read_default_value "$prompt" "$default")"
        local lower
        lower="$(echo "$raw" | tr '[:upper:]' '[:lower:]')"
        for choice in "${choices[@]}"; do
            [ "$lower" = "$choice" ] && echo "$lower" && return
        done
        echo "[demo-gate-start] Ongeldige keuze. Kies uit: ${choices[*]}" >&2
    done
}

read_preset_value() {
    local default="$1"
    if [ -z "$STARTUP_PRESET_IDS" ]; then
        echo "[demo-gate-start] Geen startup presets beschikbaar." >&2
        echo "$default"
        return
    fi
    while true; do
        local raw
        raw="$(read_default_value "Startup preset id" "$default")"
        if echo " $STARTUP_PRESET_IDS " | grep -q " ${raw} "; then
            echo "$raw"
            return
        fi
        echo "[demo-gate-start] Startup preset niet gevonden. Beschikbaar: $STARTUP_PRESET_IDS" >&2
    done
}

read_summary_preset_value() {
    local default="$1"
    if [ -z "$SUMMARY_PRESET_IDS" ]; then echo ""; return; fi
    while true; do
        local raw
        raw="$(read_default_value "Summary preset id" "$default")"
        if echo " $SUMMARY_PRESET_IDS " | grep -q " ${raw} "; then
            echo "$raw"
            return
        fi
        echo "[demo-gate-start] Summary preset niet gevonden. Beschikbaar: $SUMMARY_PRESET_IDS" >&2
    done
}

read_existing_path_value() {
    local prompt="$1"
    local default="$2"
    local expect_dir="${3:-false}"
    while true; do
        local raw
        raw="$(read_default_value "$prompt" "$default")"
        local resolved
        resolved="$(resolve_repo_path "$raw")"
        if [ -z "$resolved" ]; then
            echo "[demo-gate-start] Pad mag niet leeg zijn." >&2
            continue
        fi
        if [ ! -e "$resolved" ]; then
            echo "[demo-gate-start] Pad niet gevonden: $resolved" >&2
            continue
        fi
        if [ "$expect_dir" = "true" ] && [ ! -d "$resolved" ]; then
            echo "[demo-gate-start] Verwacht een map: $resolved" >&2
            continue
        fi
        if [ "$expect_dir" = "false" ] && [ ! -f "$resolved" ]; then
            echo "[demo-gate-start] Verwacht een bestand: $resolved" >&2
            continue
        fi
        echo "$resolved"
        return
    done
}

# Defaults voor paden
DEFAULT_SUMMARY_PRESET="${SUMMARY_PRESET_IDS%% *}"
DEFAULT_SUMMARY_SCRIPT="$(resolve_repo_path "py3_script_runner/scripts/demo_gate_summary_single_robot.json")"
DEFAULT_WORKSHOP_SCRIPT="$(resolve_repo_path "py3_script_runner/scripts/demo_gate_workshop_single_robot.json")"
DEFAULT_AUDIO_FIXTURES_ROOT="$(resolve_repo_path "py3_dialog_manager/demo_gate_audio")"

echo "[demo-gate-start] Repo: $REPO_ROOT"
echo "[demo-gate-start] Scenario's:"
echo "  - all        Meest complete controle voor een demo: doorloopt de volledige demo en test ook herstel bij storingen."
echo "  - chat       Test het gewone gesprek: luisteren, antwoorden en spraakcommando's."
echo "  - summary    Test de samenvattingsflow: SR start, DM neemt op, transcript wordt bewerkt en de samenvatting wordt afgerond."
echo "  - fallbacks  Test uitval van STT, LLM of TTS en controleert of herstel en fallback goed werken."
echo "  - rehearsal  Test de volledige demo-doorloop: gesprek, samenvatting en workshopscript achter elkaar."
[ -n "$STARTUP_PRESET_IDS" ] && echo "[demo-gate-start] Startup presets : $STARTUP_PRESET_IDS"
[ -n "$SUMMARY_PRESET_IDS" ] && echo "[demo-gate-start] Summary presets : $SUMMARY_PRESET_IDS"
echo ""

declare -A SCENARIO_MAP=(
    [all]="all" [chat]="happy_path_dialog" [summary]="summary_edit_flow"
    [fallbacks]="service_loss_recovery" [rehearsal]="full_demo_rehearsal"
    [happy_path_dialog]="happy_path_dialog" [summary_edit_flow]="summary_edit_flow"
    [service_loss_recovery]="service_loss_recovery" [full_demo_rehearsal]="full_demo_rehearsal"
)

PROFILE="offline"
SCENARIO="all"
PRESET="virtuele_robot"
SUMMARY_PRESET_ID="$DEFAULT_SUMMARY_PRESET"
SUMMARY_SCRIPT_PATH="$DEFAULT_SUMMARY_SCRIPT"
WORKSHOP_SCRIPT_PATH="$DEFAULT_WORKSHOP_SCRIPT"
AUDIO_FIXTURES_ROOT="$DEFAULT_AUDIO_FIXTURES_ROOT"
NAO_IP=""
KEEP_ARTIFACTS=false

run_default="$(read_yn_value "Run default (all scenarios, zonder services, zonder robot)" "y")"

if [ "$run_default" = "false" ]; then
    SCENARIO_RAW="$(read_choice_value "Scenario" "all" all chat summary fallbacks rehearsal)"
    SCENARIO="${SCENARIO_MAP[$SCENARIO_RAW]:-all}"

    use_live_services="$(read_yn_value "Echte services gebruiken" "n")"
    use_live_robot="$(read_yn_value "Echte robot gebruiken" "n")"

    if [ "$use_live_robot" = "true" ]; then
        [ "$use_live_services" = "false" ] && echo "[demo-gate-start] Echte robot vereist het live_robot profiel; services gaan daarmee ook live."
        PROFILE="live_robot"
    elif [ "$use_live_services" = "true" ]; then
        PROFILE="live_services"
    else
        PROFILE="offline"
    fi

    DEFAULT_PRESET="virtuele_robot"
    [ "$PROFILE" = "live_robot" ] && DEFAULT_PRESET="alex"
    PRESET="$(read_preset_value "$DEFAULT_PRESET")"
    SUMMARY_PRESET_ID="$(read_summary_preset_value "$DEFAULT_SUMMARY_PRESET")"

    custom_paths="$(read_yn_value "Custom script/audio paden instellen" "n")"
    if [ "$custom_paths" = "true" ]; then
        SUMMARY_SCRIPT_PATH="$(read_existing_path_value "Summary script pad" "$DEFAULT_SUMMARY_SCRIPT" "false")"
        WORKSHOP_SCRIPT_PATH="$(read_existing_path_value "Workshop script pad" "$DEFAULT_WORKSHOP_SCRIPT" "false")"
        AUDIO_FIXTURES_ROOT="$(read_existing_path_value "Audio fixtures map" "$DEFAULT_AUDIO_FIXTURES_ROOT" "true")"
    fi

    if [ "$PROFILE" = "live_robot" ]; then
        NAO_IP="$(read_default_value "NAO IP override (leeg = preset)" "")"
    fi

    KEEP_ARTIFACTS="$(read_yn_value "Artifacts bewaren" "n")"
    run_now="$(read_yn_value "Demo gate nu starten" "y")"
    if [ "$run_now" = "false" ]; then
        echo "[demo-gate-start] Geannuleerd."
        exit 0
    fi
else
    echo "[demo-gate-start] Default selectie: profile=offline, scenario=all"
fi

CMD_ARGS=(
    "$DEMO_GATE_SCRIPT"
    --profile "$PROFILE"
    --scenario "$SCENARIO"
    --preset "$PRESET"
    --summary-script "$SUMMARY_SCRIPT_PATH"
    --workshop-script "$WORKSHOP_SCRIPT_PATH"
    --audio-fixtures-root "$AUDIO_FIXTURES_ROOT"
)
[ -n "$SUMMARY_PRESET_ID" ] && CMD_ARGS+=(--summary-preset-id "$SUMMARY_PRESET_ID")
[ -n "$NAO_IP" ] && CMD_ARGS+=(--nao-ip "$NAO_IP")
[ "$KEEP_ARTIFACTS" = "true" ] && CMD_ARGS+=(--keep-artifacts)

echo ""
echo "[demo-gate-start] Command:"
echo "  \"$PYTHON_EXE\" ${CMD_ARGS[*]}"
echo ""

cd "$REPO_ROOT"
"$PYTHON_EXE" "${CMD_ARGS[@]}"
