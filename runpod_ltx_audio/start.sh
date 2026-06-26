#!/usr/bin/env bash
set -euo pipefail

export HF_HOME="${HF_HOME:-/workspace/models/huggingface}"
export LTX2_MODEL_PATH="${LTX2_MODEL_PATH:-/workspace/models/ltx2}"
export OUTPUT_ROOT="${OUTPUT_ROOT:-/workspace/outputs}"

mkdir -p /workspace/models "$HF_HOME" "$LTX2_MODEL_PATH" "$OUTPUT_ROOT"

"${LTX2_PYTHON:-/opt/ltx2/.venv/bin/python}" /app/runpod_ltx_audio/setup_models.py

exec "${LTX2_PYTHON:-/opt/ltx2/.venv/bin/python}" /app/runpod_ltx_audio/worker.py
