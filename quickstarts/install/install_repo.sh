#!/usr/bin/env bash
# Mac installatie-script voor NAO Studio
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DM_DIR="$REPO_ROOT/py3_dialog_manager"
SR_DIR="$REPO_ROOT/py3_script_runner"
BM_DIR="$REPO_ROOT/py3_nao_behavior_manager"
BASE_DIR="$REPO_ROOT/py2_nao_base_controller"
STORY_DIR="$REPO_ROOT/py3_story_engine"
CMD_REC_DIR="$REPO_ROOT/py3_command_recognition_train"

PIPER_MODELS_DIR="$REPO_ROOT/piper_tts_models"
PIPER_DOWNLOADER="$PIPER_MODELS_DIR/download_piper_voices.py"
PIPER_DEFAULT_VOICE="$PIPER_MODELS_DIR/nl/nl_BE/nathalie/medium/nl_BE-nathalie-medium.onnx"
VOSK_MODELS_DIR="$REPO_ROOT/models"
VOSK_DOWNLOADER="$REPO_ROOT/scripts/download_vosk_models.sh"
VOSK_DEFAULT_MODEL="$VOSK_MODELS_DIR/vosk-model-small-nl-0.22"
OLLAMA_MODELS=("gemma:2b" "granite3.2:8b")

AZURE_ENV_VARS=("AZURE_SPEECH_KEY" "AZURE_SPEECH_REGION")
OLLAMA_CLOUD_REQUIRED_ENV_VARS=("OLLAMA_API_KEY")
OLLAMA_CLOUD_OPTIONAL_ENV_VARS=("OLLAMA_HOST")

PATH_CACHE_FILE="$SCRIPT_DIR/install_paths.local.conf"

PROFILE_FILE="$HOME/.zshrc"
if [ -n "${SHELL:-}" ]; then
    case "$SHELL" in
        */bash) PROFILE_FILE="$HOME/.bash_profile" ;;
        */zsh)  PROFILE_FILE="$HOME/.zshrc" ;;
    esac
fi

# Laad eerder ingestelde waarden zodat herhaald draaien ze niet opnieuw vraagt
# Alleen export-regels evalueren, geen zsh-specifieke syntax
if [ -f "$PROFILE_FILE" ]; then
    while IFS= read -r line; do
        if [[ "$line" =~ ^export\ [A-Z_]+=\" ]]; then
            eval "$line" 2>/dev/null || true
        fi
    done < "$PROFILE_FILE"
fi

# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

to_lower() { echo "$1" | tr '[:upper:]' '[:lower:]'; }

read_yn() {
    local prompt="$1"
    local default="${2:-n}"
    local suffix="[N/y]"
    [ "$default" = "y" ] && suffix="[Y/n]"
    while true; do
        read -r -p "$prompt $suffix " raw
        local value
        value="$(to_lower "${raw:-}")"
        if [ -z "$value" ]; then
            [ "$default" = "y" ] && return 0 || return 1
        fi
        case "$value" in
            y|yes|j|ja) return 0 ;;
            n|no|nee)   return 1 ;;
            *) echo "[install] Vul y of n in." ;;
        esac
    done
}

read_plain_secret() {
    local prompt="$1"
    local value
    read -r -s -p "$prompt: " value
    echo ""
    echo "$value"
}

read_menu_choice() {
    local prompt="$1"
    local default="$2"
    shift 2
    # Remaining args: alias=value pairs
    while true; do
        read -r -p "$prompt [$default] " raw
        local value="${raw:-$default}"
        local lower
        lower="$(to_lower "$value")"
        local matched=""
        for pair in "$@"; do
            local alias="${pair%%=*}"
            local target="${pair##*=}"
            if [ "$lower" = "$alias" ]; then
                matched="$target"
                break
            fi
        done
        if [ -n "$matched" ]; then
            echo "$matched"
            return
        fi
        echo "[install] Ongeldige keuze."
    done
}

# ---------------------------------------------------------------------------
# Path cache (eenvoudig key=value bestand)
# ---------------------------------------------------------------------------

get_path_cache() {
    local key="$1"
    if [ ! -f "$PATH_CACHE_FILE" ]; then
        echo ""
        return
    fi
    grep "^${key}=" "$PATH_CACHE_FILE" 2>/dev/null | head -1 | cut -d= -f2-
}

save_path_cache() {
    local key="$1"
    local value="$2"
    local tmp
    tmp="$(mktemp)"
    grep -v "^${key}=" "$PATH_CACHE_FILE" > "$tmp" 2>/dev/null || true
    echo "${key}=${value}" >> "$tmp"
    mv "$tmp" "$PATH_CACHE_FILE"
}

# ---------------------------------------------------------------------------
# Python detectie
# ---------------------------------------------------------------------------

test_python_interpreter() {
    local python_path="$1"
    local expected_major="$2"
    local expected_minor="$3"
    if [ -z "$python_path" ] || [ ! -x "$python_path" ]; then
        return 1
    fi
    "$python_path" -c "
import sys
ok = (sys.version_info[0] == $expected_major and sys.version_info[1] >= $expected_minor)
raise SystemExit(0 if ok else 1)
" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Automatische prerequisite installatie (vereist alleen Homebrew)
# ---------------------------------------------------------------------------

# Globale Python-paden, gezet door ensure_prerequisites()
AUTO_PYTHON3=""
AUTO_PYTHON2=""

ensure_homebrew() {
    if command -v brew > /dev/null 2>&1; then
        echo "[install] Homebrew gevonden: $(command -v brew)"
        return
    fi
    echo ""
    echo "[install] FOUT: Homebrew is niet geinstalleerd."
    echo "[install] Installeer Homebrew eerst via:"
    echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo ""
    exit 1
}

ensure_python27() {
    # Vereist x86_64 Python 2.7 van python.org — die kan de x86_64 pynaoqi SDK laden.
    # De ARM64 Python van pyenv werkt NIET met de pynaoqi dylibs.
    local candidates=(
        "/Library/Frameworks/Python.framework/Versions/2.7/bin/python2.7"
        "/usr/local/bin/python2.7"
    )

    for path in "${candidates[@]}"; do
        if test_python_interpreter "$path" 2 7; then
            local arch
            arch="$(file "$path" 2>/dev/null | grep -o 'x86_64\|arm64' | head -1)"
            if [ "$arch" = "x86_64" ]; then
                echo "[install] Python 2.7 (x86_64) gevonden: $path"
                AUTO_PYTHON2="$path"
                return
            else
                echo "[install] Python 2.7 gevonden op $path maar architectuur is '$arch' — werkt niet met pynaoqi SDK (vereist x86_64)."
            fi
        fi
    done

    echo "[install] Python 2.7.18 (x86_64) niet gevonden. Downloaden van python.org..."
    echo "[install] Dit vereist een sudo-wachtwoord voor de installatie."

    # Rosetta 2 is nodig om x86_64 binaries op Apple Silicon te draaien
    if ! /usr/bin/pgrep -q oahd 2>/dev/null && [ ! -f /Library/Apple/usr/share/rosetta/rosetta ]; then
        echo "[install] Rosetta 2 installeren (nodig voor x86_64 Python op Apple Silicon)..."
        sudo softwareupdate --install-rosetta --agree-to-license
    else
        echo "[install] Rosetta 2 al aanwezig."
    fi

    local pkg_url="https://www.python.org/ftp/python/2.7.18/python-2.7.18-macosx10.9.pkg"
    local pkg_file="/tmp/python-2.7.18-macosx10.9.pkg"
    curl -L --progress-bar -o "$pkg_file" "$pkg_url"
    sudo installer -pkg "$pkg_file" -target /
    rm -f "$pkg_file"

    for path in "${candidates[@]}"; do
        if test_python_interpreter "$path" 2 7; then
            echo "[install] Python 2.7 (x86_64) geinstalleerd: $path"
            AUTO_PYTHON2="$path"
            return
        fi
    done

    echo "[install] FOUT: Python 2.7.18 installatie mislukt." >&2
    exit 1
}

configure_pynaoqi() {
    # Kijk of de SDK al geconfigureerd is
    local current
    current="$(get_env_value "QI_SDK_PREFIX")"
    if [ -n "$current" ] && [ -d "$current/lib" ] && ls "$current/lib"/*.dylib > /dev/null 2>&1; then
        echo "[install] pynaoqi SDK al geconfigureerd: $current"
        PYNAOQI_SDK_PATH="$current"
        return
    fi

    # Zoek automatisch — eerst in de repo zelf, dan op bekende externe locaties
    local sdk_path=""
    local candidates=(
        "$REPO_ROOT/build_files/pynaoqi-mac"
        "$(cd "$REPO_ROOT/.." 2>/dev/null && pwd)/pynaoqi-installation-for-mac/pynaoqi"
        "$HOME/Code/pynaoqi-installation-for-mac/pynaoqi"
    )
    for candidate in "${candidates[@]}"; do
        if [ -d "$candidate/lib" ] && ls "$candidate/lib"/*.dylib > /dev/null 2>&1; then
            sdk_path="$candidate"
            break
        fi
    done

    if [ -n "$sdk_path" ]; then
        echo "[install] pynaoqi SDK gevonden: $sdk_path"
    else
        echo ""
        echo "[install] pynaoqi SDK niet automatisch gevonden."
        echo "[install] Geef het pad op naar de pynaoqi map (de map met 'lib' en 'bin'):"
        while true; do
            read -r -p "Pad naar pynaoqi SDK: " raw
            sdk_path="${raw% }"
            sdk_path="${sdk_path/#\~/$HOME}"
            if [ -d "$sdk_path/lib" ] && ls "$sdk_path/lib"/*.dylib > /dev/null 2>&1; then
                break
            fi
            echo "[install] Niet gevonden op: $sdk_path (verwacht: $sdk_path/lib/*.dylib)"
        done
    fi

    PYNAOQI_SDK_PATH="$sdk_path"

    # Verwijder bestaand pynaoqi blok zodat we het niet dubbel zetten
    if grep -q "pynaoqi SDK (toegevoegd door install_repo.sh)" "$PROFILE_FILE" 2>/dev/null; then
        local tmp
        tmp="$(mktemp)"
        awk '
            /# pynaoqi SDK \(toegevoegd door install_repo.sh\)/ { skip=1; next }
            skip && /^$/ { skip=0; next }
            !skip { print }
        ' "$PROFILE_FILE" > "$tmp"
        mv "$tmp" "$PROFILE_FILE"
    fi

    cat >> "$PROFILE_FILE" << SDKEOF

# pynaoqi SDK (toegevoegd door install_repo.sh)
export QI_SDK_PREFIX="$sdk_path"
export PYTHONPATH="\$QI_SDK_PREFIX/lib/python2.7/site-packages\${PYTHONPATH:+:\$PYTHONPATH}"
export DYLD_LIBRARY_PATH="\$QI_SDK_PREFIX/lib\${DYLD_LIBRARY_PATH:+:\$DYLD_LIBRARY_PATH}"
SDKEOF

    export QI_SDK_PREFIX="$sdk_path"
    export PYTHONPATH="$sdk_path/lib/python2.7/site-packages${PYTHONPATH:+:$PYTHONPATH}"
    export DYLD_LIBRARY_PATH="$sdk_path/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

    echo "[install] pynaoqi SDK env vars gezet in $PROFILE_FILE."
}

ensure_python3() {
    local candidates=(
        "/opt/homebrew/bin/python3"
        "/usr/local/bin/python3"
        "python3"
        "python3.13" "python3.12" "python3.11" "python3.10"
    )
    for cmd in "${candidates[@]}"; do
        local path
        if [[ "$cmd" = /* ]]; then
            path="$cmd"
        else
            path="$(command -v "$cmd" 2>/dev/null || echo "")"
        fi
        if test_python_interpreter "$path" 3 10; then
            echo "[install] Python 3.10+ gevonden: $path"
            AUTO_PYTHON3="$path"
            return
        fi
    done

    echo "[install] Python 3 niet gevonden. Installeren via Homebrew..."
    brew install python3

    for cmd in "/opt/homebrew/bin/python3" "/usr/local/bin/python3" "python3"; do
        local path
        if [[ "$cmd" = /* ]]; then
            path="$cmd"
        else
            path="$(command -v "$cmd" 2>/dev/null || echo "")"
        fi
        if test_python_interpreter "$path" 3 10; then
            echo "[install] Python 3.10+ gevonden na install: $path"
            AUTO_PYTHON3="$path"
            return
        fi
    done

    echo "[install] FOUT: Python 3.10+ kon niet worden geinstalleerd." >&2
    exit 1
}

PYNAOQI_SDK_PATH=""

ensure_prerequisites() {
    echo "[install] Controleer en installeer prerequisites..."
    ensure_homebrew
    ensure_python3
    ensure_python27
    configure_pynaoqi
    echo "[install] Prerequisites klaar."
    echo ""
}

# ---------------------------------------------------------------------------
# Venv aanmaken en pip installeren
# ---------------------------------------------------------------------------

ensure_py3_venv() {
    local project_dir="$1"
    local python3_path="$2"
    local venv_python="$project_dir/venv/bin/python"
    if [ -f "$venv_python" ]; then
        echo "[install] Gebruik bestaande venv: $project_dir/venv" >&2
        echo "$venv_python"
        return
    fi
    echo "[install] Maak Py3 venv: $project_dir/venv" >&2
    "$python3_path" -m venv "$project_dir/venv"
    echo "$venv_python"
}

ensure_py2_venv() {
    local project_dir="$1"
    local python2_path="$2"
    local venv_python="$project_dir/venv/bin/python"
    if [ -f "$venv_python" ]; then
        echo "[install] Gebruik bestaande Py2 venv: $project_dir/venv" >&2
        echo "$venv_python"
        return
    fi
    echo "[install] Maak Py2 venv: $project_dir/venv" >&2
    "$python2_path" -m virtualenv "$project_dir/venv" 2>/dev/null || {
        echo "[install] virtualenv ontbreekt mogelijk in Python2; probeer installatie..." >&2
        "$python2_path" -m pip install virtualenv
        "$python2_path" -m virtualenv "$project_dir/venv"
    }
    echo "$venv_python"
}

invoke_pip_install() {
    local python_path="$1"
    local work_dir="$2"
    shift 2
    # Remaining args: pip arguments
    pushd "$work_dir" > /dev/null
    "$python_path" -m pip "$@"
    popd > /dev/null
}

install_py3_project_from_requirements() {
    local project_dir="$1"
    local python3_path="$2"
    local include_tests="$3"
    local venv_python
    venv_python="$(ensure_py3_venv "$project_dir" "$python3_path")"
    invoke_pip_install "$venv_python" "$project_dir" install --upgrade pip >&2
    invoke_pip_install "$venv_python" "$project_dir" install -r requirements.txt >&2
    if [ "$include_tests" = "true" ]; then
        invoke_pip_install "$venv_python" "$project_dir" install pytest >&2
    fi
    echo "$venv_python"
}

install_story_engine() {
    local python3_path="$1"
    local include_tests="$2"
    local venv_python
    venv_python="$(ensure_py3_venv "$STORY_DIR" "$python3_path")"
    invoke_pip_install "$venv_python" "$STORY_DIR" install --upgrade pip
    invoke_pip_install "$venv_python" "$STORY_DIR" install -e .
    if [ "$include_tests" = "true" ] && [ -f "$STORY_DIR/requirements.txt" ]; then
        invoke_pip_install "$venv_python" "$STORY_DIR" install -r requirements.txt
    fi
}

install_cmd_rec_package() {
    local python3_path="$1"
    local include_tests="$2"
    local venv_python
    venv_python="$(ensure_py3_venv "$CMD_REC_DIR" "$python3_path")"
    invoke_pip_install "$venv_python" "$CMD_REC_DIR" install --upgrade pip
    if [ "$include_tests" = "true" ]; then
        invoke_pip_install "$venv_python" "$CMD_REC_DIR" install -e ".[test]"
    else
        invoke_pip_install "$venv_python" "$CMD_REC_DIR" install -e .
    fi
}

install_base_controller() {
    local python2_path="$1"
    local venv_python
    venv_python="$(ensure_py2_venv "$BASE_DIR" "$python2_path")"
    invoke_pip_install "$venv_python" "$BASE_DIR" install --upgrade pip
    invoke_pip_install "$venv_python" "$BASE_DIR" install -r requirements.txt
}

# ---------------------------------------------------------------------------
# Omgevingsvariabelen in ~/.zshrc (of ~/.bash_profile)
# ---------------------------------------------------------------------------

get_env_value() {
    local name="$1"
    # Kijk eerst in lopende sessie, dan in profielbestand
    local val="${!name:-}"
    if [ -n "$val" ]; then echo "$val"; return; fi
    if [ -f "$PROFILE_FILE" ]; then
        val="$(grep "^export ${name}=" "$PROFILE_FILE" 2>/dev/null | tail -1 | sed 's/^export [^=]*=//;s/^"//;s/"$//')"
    fi
    echo "${val:-}"
}

set_profile_export() {
    local name="$1"
    local value="$2"
    touch "$PROFILE_FILE"
    local tmp
    tmp="$(mktemp)"
    grep -v "^export ${name}=" "$PROFILE_FILE" > "$tmp" 2>/dev/null || true
    if [ -n "$value" ]; then
        echo "export ${name}=\"${value}\"" >> "$tmp"
    fi
    mv "$tmp" "$PROFILE_FILE"
    if [ -n "$value" ]; then
        export "${name}=${value}"
    fi
}

test_env_group_complete() {
    # Args: env var names
    for name in "$@"; do
        local val
        val="$(get_env_value "$name")"
        if [ -z "$val" ]; then return 1; fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# Piper stem
# ---------------------------------------------------------------------------

ensure_piper_voice() {
    local dm_venv_python="$1"
    if [ -f "$PIPER_DEFAULT_VOICE" ]; then
        echo "[install] Piper voice al aanwezig: $PIPER_DEFAULT_VOICE"
        return
    fi
    if [ ! -f "$PIPER_DOWNLOADER" ]; then
        echo "[install] Piper downloader ontbreekt: $PIPER_DOWNLOADER"
        return
    fi
    invoke_pip_install "$dm_venv_python" "$DM_DIR" install huggingface_hub || {
        echo "[install] Piper voice download faalde (huggingface_hub install)."
        return
    }
    pushd "$PIPER_MODELS_DIR" > /dev/null
    "$dm_venv_python" "$PIPER_DOWNLOADER" || {
        echo "[install] Piper voice download faalde."
        popd > /dev/null
        return
    }
    popd > /dev/null
    if [ -f "$PIPER_DEFAULT_VOICE" ]; then
        echo "[install] Piper voice download klaar."
    else
        echo "[install] Piper voice is na download nog niet gevonden: $PIPER_DEFAULT_VOICE"
    fi
}

# ---------------------------------------------------------------------------
# Vosk modellen
# ---------------------------------------------------------------------------

ensure_vosk_models() {
    if [ -d "$VOSK_DEFAULT_MODEL" ]; then
        echo "[install] Vosk NL model al aanwezig: $VOSK_DEFAULT_MODEL"
        return
    fi
    if [ ! -f "$VOSK_DOWNLOADER" ]; then
        echo "[install] Vosk downloader ontbreekt: $VOSK_DOWNLOADER"
        return
    fi
    bash "$VOSK_DOWNLOADER" "$VOSK_MODELS_DIR" || {
        echo "[install] Vosk model download faalde."
        return
    }
    if [ -d "$VOSK_DEFAULT_MODEL" ]; then
        echo "[install] Vosk model download klaar."
    else
        echo "[install] Vosk model is na download nog niet gevonden: $VOSK_DEFAULT_MODEL"
    fi
}

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

resolve_ollama_command() {
    command -v ollama 2>/dev/null || echo ""
}

test_ollama_model_installed() {
    local ollama_cmd="$1"
    local model="$2"
    if [ -z "$ollama_cmd" ]; then return 1; fi
    "$ollama_cmd" list 2>/dev/null | awk '{print $1}' | grep -qx "$model"
}

get_missing_ollama_models() {
    local ollama_cmd="$1"
    local missing=()
    for model in "${OLLAMA_MODELS[@]}"; do
        if ! test_ollama_model_installed "$ollama_cmd" "$model"; then
            missing+=("$model")
        fi
    done
    echo "${missing[@]:-}"
}

ensure_ollama_cli() {
    local ollama_cmd
    ollama_cmd="$(resolve_ollama_command)"
    if [ -n "$ollama_cmd" ]; then
        echo "[install] Ollama CLI gevonden: $ollama_cmd" >&2
        echo "$ollama_cmd"
        return
    fi

    if command -v brew > /dev/null 2>&1; then
        echo "[install] Probeer Ollama te installeren via Homebrew..." >&2
        brew install ollama || echo "[install] brew install ollama faalde." >&2
    fi

    ollama_cmd="$(resolve_ollama_command)"
    if [ -n "$ollama_cmd" ]; then
        echo "[install] Ollama CLI gevonden na install: $ollama_cmd" >&2
        echo "$ollama_cmd"
        # Start Ollama service zodat pull meteen werkt
        ensure_ollama_running "$ollama_cmd"
        return
    fi

    echo "" >&2
    echo "Ollama is nog niet gevonden." >&2
    echo "Installeer Ollama handmatig via https://ollama.com/download/mac." >&2
    echo "Als de installer Ollama daarna nog niet ziet, sluit dit venster en start de installer opnieuw." >&2
    read -r -p "Druk op ENTER om door te gaan..."

    ollama_cmd="$(resolve_ollama_command)"
    if [ -n "$ollama_cmd" ]; then
        echo "[install] Ollama CLI gevonden na handmatige stap: $ollama_cmd" >&2
    else
        echo "[install] Ollama CLI nog steeds niet gevonden." >&2
    fi
    echo "${ollama_cmd:-}"
}

ensure_ollama_running() {
    local ollama_cmd="$1"
    if pgrep -x ollama > /dev/null 2>&1; then
        echo "[install] Ollama server al actief."
        return
    fi
    echo "[install] Ollama server starten..."
    if command -v brew > /dev/null 2>&1; then
        brew services start ollama 2>/dev/null || "$ollama_cmd" serve > /dev/null 2>&1 &
    else
        "$ollama_cmd" serve > /dev/null 2>&1 &
    fi
    # Wacht tot de server bereikbaar is (max 15s)
    local i=0
    while [ $i -lt 15 ]; do
        "$ollama_cmd" list > /dev/null 2>&1 && break
        sleep 1
        i=$(( i + 1 ))
    done
    echo "[install] Ollama server gereed."
}

ensure_ollama_models() {
    local ollama_cmd="$1"
    shift
    local models=("$@")
    if [ -z "$ollama_cmd" ]; then
        echo "[install] Ollama CLI niet gevonden; lokale modellen worden overgeslagen."
        return
    fi
    if [ "${#models[@]}" -eq 0 ]; then
        echo "[install] Geen ontbrekende Ollama modellen te installeren."
        return
    fi
    ensure_ollama_running "$ollama_cmd"
    for model in "${models[@]}"; do
        echo "[install] ollama pull $model"
        "$ollama_cmd" pull "$model" || echo "[install] ollama pull faalde voor model: $model"
    done
}

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

show_azure_explainer() {
    echo ""
    echo "Azure cloud services worden hier gebruikt voor cloud speech."
    echo "Daarvoor heb je minimaal een Speech key en region nodig."
    echo "Maak of open een Speech resource in de Azure portal: https://portal.azure.com/"
    echo "Open daar je Speech resource en ga naar 'Keys and Endpoint'."
    echo "Daar vind je je key; je region is de region van die resource."
    echo "Officiele uitleg: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-recognize-speech"
    echo "Deze installer zet die waarden in $PROFILE_FILE."
    echo ""
}

show_ollama_cloud_explainer() {
    echo ""
    echo "Ollama cloud gebruikt OLLAMA_API_KEY om een remote Ollama endpoint te bereiken."
    echo "OLLAMA_HOST is optioneel en alleen nodig als je niet de standaard host gebruikt."
    echo "Maak een API key aan in je Ollama account: https://ollama.com/settings/keys"
    echo "Officiele authenticatie-uitleg: https://docs.ollama.com/api/authentication"
    echo "Officiele API/base-url uitleg: https://docs.ollama.com/api/introduction"
    echo "Deze installer zet die waarden in $PROFILE_FILE."
    echo ""
}

configure_credentials() {
    local force_prompt="${1:-false}"

    local azure_complete=false
    test_env_group_complete "${AZURE_ENV_VARS[@]}" && azure_complete=true

    local ollama_cloud_complete=false
    test_env_group_complete "${OLLAMA_CLOUD_REQUIRED_ENV_VARS[@]}" && ollama_cloud_complete=true

    if [ "$force_prompt" = "true" ]; then
        echo ""
        echo "Je werkt alleen de cloud credentials bij."
        echo "Bestaande waarden blijven staan totdat je ze hier vervangt."
        echo "De installer zet aangepaste waarden in $PROFILE_FILE."
        echo ""
    elif [ "$azure_complete" = "false" ] || [ "$ollama_cloud_complete" = "false" ]; then
        echo ""
        echo "Ik zie dat nog niet alle omgevingsvariabelen op de computer staan ingesteld."
        echo "Zonder die variabelen werken de cloud services niet."
        echo "Als je deze tooling hebt ontvangen van mij dan heb ik je hier instructies over gegeven."
        echo "Als je deze software van git hebt dan moet je zelf bij ollama en azure de juiste accounts aanmaken."
        echo "Je kunt de keys zelf in je profiel zetten of via dit script, dan wordt het automatisch erin gezet."
        echo "Geen zorgen het wordt nergens anders gezet, die keys blijven van jou en jou alleen."
        echo ""
    fi

    local configure_azure=false
    if [ "$azure_complete" = "false" ]; then
        read_yn "Wil je uitleg over Azure cloud services?" "n" && show_azure_explainer || true
        read_yn "Wil je Azure keys aangeven via dit script?" "y" && configure_azure=true || true
    elif [ "$force_prompt" = "true" ] || read_yn "Azure cloud variabelen zijn al aanwezig. Wil je ze aanpassen?" "n"; then
        read_yn "Wil je uitleg over Azure cloud services?" "n" && show_azure_explainer || true
        read_yn "Wil je Azure keys aangeven via dit script?" "y" && configure_azure=true || true
    fi

    if [ "$configure_azure" = "true" ]; then
        local azure_speech_key
        azure_speech_key="$(read_plain_secret "AZURE_SPEECH_KEY")"
        read -r -p "AZURE_SPEECH_REGION: " azure_speech_region
        set_profile_export "AZURE_SPEECH_KEY" "$azure_speech_key"
        set_profile_export "AZURE_SPEECH_REGION" "$azure_speech_region"
        echo "[install] Azure variabelen zijn gezet in $PROFILE_FILE."
    fi

    local configure_ollama_cloud=false
    if [ "$ollama_cloud_complete" = "false" ]; then
        read_yn "Wil je uitleg over Ollama cloud?" "n" && show_ollama_cloud_explainer || true
        read_yn "Wil je Ollama keys aangeven via dit script?" "y" && configure_ollama_cloud=true || true
    elif [ "$force_prompt" = "true" ] || read_yn "Ollama cloud variabelen zijn al aanwezig. Wil je ze aanpassen?" "n"; then
        read_yn "Wil je uitleg over Ollama cloud?" "n" && show_ollama_cloud_explainer || true
        read_yn "Wil je Ollama keys aangeven via dit script?" "y" && configure_ollama_cloud=true || true
    fi

    if [ "$configure_ollama_cloud" = "true" ]; then
        local ollama_api_key
        ollama_api_key="$(read_plain_secret "OLLAMA_API_KEY")"
        read -r -p "OLLAMA_HOST (optioneel; leeg = standaard host gebruiken): " ollama_host
        set_profile_export "OLLAMA_API_KEY" "$ollama_api_key"
        set_profile_export "OLLAMA_HOST" "${ollama_host:-}"
        echo "[install] Ollama cloud variabelen zijn gezet in $PROFILE_FILE."
    fi
}

# ---------------------------------------------------------------------------
# Verificatie
# ---------------------------------------------------------------------------

verify_install() {
    local show_missing_paths="${1:-false}"
    local missing_packages=()
    local missing_details=()

    local qi_sdk
    qi_sdk="${PYNAOQI_SDK_PATH:-$(get_env_value "QI_SDK_PREFIX")}"

    declare -A checks=(
        ["DM venv"]="$DM_DIR/venv/bin/python"
        ["SR venv"]="$SR_DIR/venv/bin/python"
        ["Behavior manager venv"]="$BM_DIR/venv/bin/python"
        ["Story engine venv"]="$STORY_DIR/venv/bin/python"
        ["Base controller venv"]="$BASE_DIR/venv/bin/python"
        ["CmdRec venv"]="$CMD_REC_DIR/venv/bin/python"
        ["Piper NL voice"]="$PIPER_DEFAULT_VOICE"
        ["Vosk NL model"]="$VOSK_DEFAULT_MODEL"
        ["pynaoqi SDK"]="${qi_sdk:-NIET_GECONFIGUREERD}/lib"
    )

    for label in "${!checks[@]}"; do
        local path="${checks[$label]}"
        if [ ! -e "$path" ]; then
            missing_packages+=("$label")
            missing_details+=("$label: $path")
        fi
    done

    local missing_env=()
    for name in "${AZURE_ENV_VARS[@]}" "${OLLAMA_CLOUD_REQUIRED_ENV_VARS[@]}"; do
        local val
        val="$(get_env_value "$name")"
        if [ -z "$val" ]; then
            missing_env+=("$name")
        fi
    done

    local missing_ollama=()
    local ollama_cmd
    ollama_cmd="$(resolve_ollama_command)"
    if [ -z "$ollama_cmd" ]; then
        missing_ollama+=("Ollama CLI")
    else
        local missing_models
        read -ra missing_models <<< "$(get_missing_ollama_models "$ollama_cmd")"
        missing_ollama+=("${missing_models[@]:-}")
    fi

    if [ "${#missing_packages[@]}" -eq 0 ] && [ "${#missing_env[@]}" -eq 0 ] && [ "${#missing_ollama[@]}" -eq 0 ]; then
        echo "[verify] Alles lijkt geinstalleerd en geconfigureerd."
        return
    fi

    if [ "${#missing_packages[@]}" -eq 0 ]; then
        echo "[verify] Libraries en lokale modellen: compleet."
    else
        echo "[verify] Er missen nog libraries of lokale modellen: ${missing_packages[*]}"
    fi

    if [ "${#missing_env[@]}" -eq 0 ]; then
        echo "[verify] Cloud variabelen: compleet."
    else
        echo "[verify] Er missen nog cloud variabelen: ${missing_env[*]}"
    fi

    local ollama_host_val
    ollama_host_val="$(get_env_value "OLLAMA_HOST")"
    if [ -z "$ollama_host_val" ]; then
        echo "[verify] OLLAMA_HOST niet gezet; standaard host wordt gebruikt."
    fi

    if [ "${#missing_ollama[@]}" -eq 0 ] || [ -z "${missing_ollama[*]:-}" ]; then
        echo "[verify] Ollama: compleet."
    else
        echo "[verify] Er missen nog Ollama onderdelen: ${missing_ollama[*]}"
    fi

    if [ "$show_missing_paths" = "true" ] && [ "${#missing_details[@]}" -gt 0 ]; then
        echo "[verify] Missende paden:"
        for detail in "${missing_details[@]}"; do
            echo "  - $detail"
        done
    fi
}

# ---------------------------------------------------------------------------
# Runtime installatie
# ---------------------------------------------------------------------------

install_runtime() {
    local include_tests="$1"
    local python3_path="$2"
    local python2_path="$3"

    echo "[install] Runtime setup start."
    local dm_python
    dm_python="$(install_py3_project_from_requirements "$DM_DIR" "$python3_path" "$include_tests")"
    install_py3_project_from_requirements "$SR_DIR" "$python3_path" "$include_tests" > /dev/null
    install_py3_project_from_requirements "$BM_DIR" "$python3_path" "$include_tests" > /dev/null
    install_story_engine "$python3_path" "$include_tests"
    install_cmd_rec_package "$python3_path" "$include_tests"
    install_base_controller "$python2_path"
    ensure_vosk_models
    ensure_piper_voice "$dm_python"
    echo "[install] Runtime setup klaar."
}

# ---------------------------------------------------------------------------
# Installatieplan samenstellen en uitvoeren
# ---------------------------------------------------------------------------

collect_install_plan() {
    echo "Om te weten hoe we alles goed installeren eerst even wat vragen."
    echo ""

    local profile
    profile="$(read_menu_choice \
        "Welk profiel wil je installeren? [gebruiker / ontwikkelaar]" \
        "gebruiker" \
        "gebruiker=gebruiker" "g=gebruiker" \
        "ontwikkelaar=ontwikkelaar" "o=ontwikkelaar" "developer=ontwikkelaar" "dev=ontwikkelaar")"

    if [ "$profile" = "gebruiker" ]; then
        echo "Mooi! Dan installeren we alleen de modules die nodig zijn om de applicatie te draaien."
    else
        echo "OK! Dan installeren we alles. Zowel de runtime als de aanvullende tooling om te testen en verder te ontwikkelen."
    fi
    echo ""

    # Python is al klaargemaakt door ensure_prerequisites()
    echo "[install] Python3: $AUTO_PYTHON3"
    echo "[install] Python2: $AUTO_PYTHON2"
    echo ""

    local ollama_cmd
    ollama_cmd="$(resolve_ollama_command)"
    local install_ollama=false
    local install_ollama_models=false
    local ollama_models_to_install=()

    if [ -z "$ollama_cmd" ]; then
        echo "Ik zie dat je Ollama nog niet hebt geinstalleerd."
        echo ""
        echo "Ollama is tooling om lokale AI modellen op je eigen computer te draaien."
        echo "Als je alleen via cloud werkt is Ollama optioneel."
        echo "Voor lokale modellen en voor lokale modelkeuze in de UI is de Ollama CLI wel nodig."
        echo "Handmatig installeren kan via: https://ollama.com/download/mac"
        echo ""
        read_yn "Wil je Ollama installeren?" "y" && install_ollama=true || true
        if [ "$install_ollama" = "true" ]; then
            ollama_models_to_install=("${OLLAMA_MODELS[@]}")
            read_yn "Wil je de lokale modellen installeren? (${OLLAMA_MODELS[*]})" "y" \
                && install_ollama_models=true || true
        fi
    else
        echo "[install] Ollama CLI gevonden: $ollama_cmd"
        local missing_raw
        missing_raw="$(get_missing_ollama_models "$ollama_cmd")"
        if [ -z "$missing_raw" ]; then
            echo "[install] Lokale Ollama modellen zijn al aanwezig: ${OLLAMA_MODELS[*]}"
        else
            read -ra ollama_models_to_install <<< "$missing_raw"
            read_yn "Wil je de ontbrekende lokale modellen installeren? (${ollama_models_to_install[*]})" "y" \
                && install_ollama_models=true || true
        fi
    fi
    echo ""

    # Exporteer plan als globale variabelen (bash heeft geen return-hashtables)
    PLAN_PROFILE="$profile"
    PLAN_INCLUDE_TESTS="$([ "$profile" = "ontwikkelaar" ] && echo true || echo false)"
    PLAN_PYTHON3="$AUTO_PYTHON3"
    PLAN_PYTHON2="$AUTO_PYTHON2"
    PLAN_INSTALL_OLLAMA="$install_ollama"
    PLAN_INSTALL_OLLAMA_MODELS="$install_ollama_models"
    PLAN_OLLAMA_MODELS_TO_INSTALL=("${ollama_models_to_install[@]:-}")
}

execute_install_plan() {
    configure_credentials "false"

    echo ""
    echo "We gaan nu alle dependencies installeren, een moment geduld. Rome is ook niet in een dag gebouwd."
    echo ""

    install_runtime "$PLAN_INCLUDE_TESTS" "$PLAN_PYTHON3" "$PLAN_PYTHON2"

    local ollama_cmd
    ollama_cmd="$(resolve_ollama_command)"

    if [ "$PLAN_INSTALL_OLLAMA" = "true" ]; then
        ollama_cmd="$(ensure_ollama_cli)"
        if [ "$PLAN_INSTALL_OLLAMA_MODELS" = "true" ]; then
            local missing_raw
            missing_raw="$(get_missing_ollama_models "${ollama_cmd:-}")"
            read -ra PLAN_OLLAMA_MODELS_TO_INSTALL <<< "${missing_raw:-}"
        fi
    fi

    if [ "$PLAN_INSTALL_OLLAMA_MODELS" = "true" ] && [ "${#PLAN_OLLAMA_MODELS_TO_INSTALL[@]}" -gt 0 ]; then
        ensure_ollama_models "${ollama_cmd:-}" "${PLAN_OLLAMA_MODELS_TO_INSTALL[@]}"
    fi

    echo ""
    verify_install "false"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

echo "Ugh!"
echo "Welkom bij de installer voor NAO Studio. Een set tools om interactie te hebben met een fysieke NAO v5 robot of de virtuele avatar daarvan."
echo ""

mode="$(read_menu_choice \
    "Wat wil je doen? [installeren / alleen verifieren / alleen credentials bijwerken]" \
    "installeren" \
    "installeren=installeren" "install=installeren" "i=installeren" \
    "verifieren=verifieren" "verify=verifieren" "v=verifieren" "alleen verifieren=verifieren" \
    "credentials=credentials" "c=credentials" "alleen credentials bijwerken=credentials")"

case "$mode" in
    verifieren)
        verify_install "false"
        read_yn "Wil je ook de missende paden zien?" "n" && verify_install "true" || true
        ;;
    credentials)
        configure_credentials "true"
        ;;
    installeren)
        ensure_prerequisites
        collect_install_plan
        execute_install_plan
        ;;
esac

echo ""
echo "[install] Klaar."
echo ""
if [ "$mode" = "credentials" ]; then
    echo "BELANGRIJK: Herstart draaiende DM/Script Runner processen zodat ze de nieuwe keys gebruiken."
    echo "Voor nieuwe terminalvensters staan de waarden klaar in $PROFILE_FILE."
    echo "In dit venster kun je direct 'source $PROFILE_FILE' uitvoeren als je hier verder werkt."
elif [ "$mode" = "installeren" ]; then
    echo "BELANGRIJK: Open een nieuw terminal-venster (of voer 'source $PROFILE_FILE' uit)"
    echo "om de ingestelde omgevingsvariabelen te activeren."
fi
