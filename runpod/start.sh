#!/usr/bin/env bash
set -euo pipefail

export HF_HOME="${HF_HOME:-/workspace/models/huggingface}"
export LTX_MODEL_PATH="${LTX_MODEL_PATH:-/workspace/models/ltx}"
export MUSETALK_MODEL_PATH="${MUSETALK_MODEL_PATH:-/workspace/models/musetalk}"
export OUTPUT_ROOT="${OUTPUT_ROOT:-/workspace/outputs}"

mkdir -p /workspace/models "$HF_HOME" "$LTX_MODEL_PATH" "$MUSETALK_MODEL_PATH" "$OUTPUT_ROOT"

python /app/runpod/setup_models.py

# MuseTalk resolves several component paths relative to its repository.
rm -rf /opt/musetalk/models
ln -s "$MUSETALK_MODEL_PATH" /opt/musetalk/models

exec python /app/runpod/worker.py
