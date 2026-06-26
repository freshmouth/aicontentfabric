FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/workspace/models/huggingface \
    LTX2_MODEL_PATH=/workspace/models/ltx2 \
    OUTPUT_ROOT=/workspace/outputs \
    LTX2_REPO_PATH=/opt/ltx2 \
    LTX2_PYTHON=/opt/ltx2/.venv/bin/python \
    RUNPOD_SERVERLESS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    ffmpeg \
    git \
    libgl1 \
    libglib2.0-0 \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

ARG LTX2_REPO_URL=https://github.com/Lightricks/LTX-2.git
ARG LTX2_GIT_REF=main

RUN git init /opt/ltx2 && \
    git -C /opt/ltx2 remote add origin "${LTX2_REPO_URL}" && \
    git -C /opt/ltx2 fetch --depth 1 origin "${LTX2_GIT_REF}" && \
    git -C /opt/ltx2 checkout --detach FETCH_HEAD

WORKDIR /opt/ltx2
RUN /root/.local/bin/uv sync --frozen

WORKDIR /app/runpod_ltx_audio
COPY requirements.txt /app/runpod_ltx_audio/requirements.txt
RUN "${LTX2_PYTHON}" -m pip install --upgrade pip setuptools wheel && \
    "${LTX2_PYTHON}" -m pip install -r /app/runpod_ltx_audio/requirements.txt

COPY start.sh setup_models.py worker.py /app/runpod_ltx_audio/
RUN chmod +x /app/runpod_ltx_audio/start.sh

EXPOSE 8000
CMD ["/app/runpod_ltx_audio/start.sh"]
