FROM pytorch/pytorch:2.1.2-cuda11.8-cudnn8-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/workspace/models/huggingface \
    LTX_MODEL_PATH=/workspace/models/ltx \
    MUSETALK_MODEL_PATH=/workspace/models/musetalk \
    OUTPUT_ROOT=/workspace/outputs \
    LTX_REPO_PATH=/opt/ltx-video \
    MUSETALK_REPO_PATH=/opt/musetalk \
    LTX_PYTHON=/opt/venvs/ltx/bin/python \
    MUSETALK_PYTHON=/opt/venvs/musetalk/bin/python \
    RUNPOD_SERVERLESS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    git \
    libgl1 \
    libglib2.0-0 \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/runpod/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install -r /app/runpod/requirements.txt

ARG LTX_REPO_URL=https://github.com/Lightricks/LTX-Video.git
ARG LTX_GIT_REF=main
ARG MUSETALK_REPO_URL=https://github.com/TMElyralab/MuseTalk.git
ARG MUSETALK_GIT_REF=main

RUN git clone --filter=blob:none --branch "${LTX_GIT_REF}" "${LTX_REPO_URL}" /opt/ltx-video && \
    git clone --filter=blob:none --branch "${MUSETALK_GIT_REF}" "${MUSETALK_REPO_URL}" /opt/musetalk

# The projects require incompatible Transformers versions. Separate virtual
# environments share the base image's CUDA/Torch packages but isolate Python deps.
RUN python -m venv --system-site-packages /opt/venvs/ltx && \
    /opt/venvs/ltx/bin/pip install --upgrade pip setuptools wheel && \
    /opt/venvs/ltx/bin/pip install -e '/opt/ltx-video[inference]'

RUN python -m venv --system-site-packages /opt/venvs/musetalk && \
    /opt/venvs/musetalk/bin/pip install --upgrade pip setuptools wheel && \
    /opt/venvs/musetalk/bin/pip install -r /opt/musetalk/requirements.txt && \
    /opt/venvs/musetalk/bin/pip install --no-cache-dir -U openmim && \
    /opt/venvs/musetalk/bin/mim install mmengine && \
    /opt/venvs/musetalk/bin/mim install 'mmcv==2.0.1' && \
    /opt/venvs/musetalk/bin/mim install 'mmdet==3.1.0' && \
    /opt/venvs/musetalk/bin/mim install 'mmpose==1.1.0'

COPY start.sh \
    setup_models.py \
    worker.py \
    ltx_image_to_video.yaml \
    /app/runpod/
RUN chmod +x /app/runpod/start.sh

WORKDIR /app/runpod
EXPOSE 8000
CMD ["/app/runpod/start.sh"]
