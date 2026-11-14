#!/usr/bin/env bash
# Download wake word models into /opt/voicebot/models (run as the code owner, not root).
set -euo pipefail

DIR=${1:-/opt/voicebot/models}
OWW=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1
VOSK=https://alphacephei.com/vosk/models

mkdir -p "$DIR/openwakeword"
for f in hey_jarvis_v0.1.onnx melspectrogram.onnx embedding_model.onnx; do
  [[ -s $DIR/openwakeword/$f ]] || curl -fsSL -o "$DIR/openwakeword/$f" "$OWW/$f"
done

for m in vosk-model-small-ru-0.22; do
  if [[ ! -d $DIR/$m ]]; then
    curl -fsSL -o "$DIR/$m.zip" "$VOSK/$m.zip"
    (cd "$DIR" && unzip -q "$m.zip" && rm "$m.zip")
  fi
done

chmod -R o-w "$DIR"
du -sh "$DIR"/* 
