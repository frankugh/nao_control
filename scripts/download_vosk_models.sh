#!/usr/bin/env bash
set -euo pipefail

MODELS_DIR="${1:-models}"
mkdir -p "$MODELS_DIR"

models=(
    "vosk-model-small-nl-0.22|https://alphacephei.com/vosk/models/vosk-model-small-nl-0.22.zip"
    "vosk-model-nl-spraakherkenning-0.6|https://alphacephei.com/vosk/models/vosk-model-nl-spraakherkenning-0.6.zip"
    "vosk-model-nl-spraakherkenning-0.6-lgraph|https://alphacephei.com/vosk/models/vosk-model-nl-spraakherkenning-0.6-lgraph.zip"
)

for entry in "${models[@]}"; do
    name="${entry%%|*}"
    url="${entry##*|}"
    dest="$MODELS_DIR/$name"

    if [ -d "$dest" ]; then
        echo "Skip: $name bestaat al in $MODELS_DIR"
        continue
    fi

    zip="$MODELS_DIR/${name}.zip"
    echo "Download: $name"
    curl -L --progress-bar -o "$zip" "$url"

    echo "Uitpakken: $name"
    unzip -q "$zip" -d "$MODELS_DIR"
    rm -f "$zip"
    echo "Klaar: $name"
done

echo "Klaar."
