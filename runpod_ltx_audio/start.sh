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
export LOG_ROOT="${LOG_ROOT:-${DEFAULT_PERSIST_ROOT}/logs}"
export SETUP_LOG_PATH="${SETUP_LOG_PATH:-${LOG_ROOT}/ltx2_audio_setup.log}"
export SETUP_STATUS_PATH="${SETUP_STATUS_PATH:-${LOG_ROOT}/ltx2_audio_setup.status}"

mkdir -p "${DEFAULT_PERSIST_ROOT}/models" "$HF_HOME" "$LTX2_MODEL_PATH" "$OUTPUT_ROOT" "$LOG_ROOT"

run_setup() {
  set +e
  echo "started $(date -Is)" > "$SETUP_STATUS_PATH"
  "${LTX2_PYTHON:-/opt/ltx2/.venv/bin/python}" /app/runpod_ltx_audio/setup_models.py >> "$SETUP_LOG_PATH" 2>&1
  exit_code=$?
  if [[ "$exit_code" -eq 0 ]]; then
    echo "succeeded $(date -Is)" > "$SETUP_STATUS_PATH"
  else
    echo "failed:${exit_code} $(date -Is)" > "$SETUP_STATUS_PATH"
  fi
  return "$exit_code"
}

if [[ "${RUNPOD_SERVERLESS:-1}" =~ ^(1|true|yes|on)$ ]]; then
  run_setup
else
  run_setup &
fi

exec "${LTX2_PYTHON:-/opt/ltx2/.venv/bin/python}" /app/runpod_ltx_audio/worker.py
