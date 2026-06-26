#!/usr/bin/env bash
set -euo pipefail

if [[ -d /runpod-volume ]]; then
  DEFAULT_PERSIST_ROOT="/runpod-volume"
else
  DEFAULT_PERSIST_ROOT="/workspace"
fi

export HF_HOME="${HF_HOME:-${DEFAULT_PERSIST_ROOT}/models/huggingface}"
export LTX2_MODEL_PATH="${LTX2_MODEL_PATH:-${DEFAULT_PERSIST_ROOT}/models/ltx2}"
export OUTPUT_ROOT="${OUTPUT_ROOT:-${DEFAULT_PERSIST_ROOT}/outputs}"

mkdir -p "${DEFAULT_PERSIST_ROOT}/models" "$HF_HOME" "$LTX2_MODEL_PATH" "$OUTPUT_ROOT"

"${LTX2_PYTHON:-/opt/ltx2/.venv/bin/python}" /app/runpod_ltx_audio/setup_models.py

exec "${LTX2_PYTHON:-/opt/ltx2/.venv/bin/python}" /app/runpod_ltx_audio/worker.py
