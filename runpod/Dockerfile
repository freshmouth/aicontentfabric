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
    build-essential \
    ca-certificates \
    curl \
    ffmpeg \
    git \
    libgl1 \
    libglib2.0-0 \
    ninja-build \
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
    /opt/venvs/ltx/bin/pip install -e '/opt/ltx-video[inference]' && \
    /opt/venvs/ltx/bin/pip install \
        torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
        --index-url https://download.pytorch.org/whl/cu118 && \
    /opt/venvs/ltx/bin/python -c \
        "import torch; assert torch.__version__.startswith('2.5.1'); assert hasattr(torch, 'xpu')"

RUN python -m venv --system-site-packages /opt/venvs/musetalk && \
    /opt/venvs/musetalk/bin/pip install --upgrade pip 'setuptools<81' wheel && \
    /opt/venvs/musetalk/bin/pip install \
        torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2 \
        --index-url https://download.pytorch.org/whl/cu118 && \
    /opt/venvs/musetalk/bin/pip install -r /opt/musetalk/requirements.txt && \
    /opt/venvs/musetalk/bin/pip install 'setuptools<81' 'mmengine>=0.7.1,<1.0.0' && \
    /opt/venvs/musetalk/bin/pip install \
        'mmcv==2.0.1' \
        --find-links https://download.openmmlab.com/mmcv/dist/cu118/torch2.0/index.html && \
    /opt/venvs/musetalk/bin/pip install --no-build-isolation 'chumpy==0.70' && \
    /opt/venvs/musetalk/bin/pip install 'mmdet==3.1.0' 'mmpose==1.1.0' && \
    /opt/venvs/musetalk/bin/python -c \
        "import mmcv, mmdet, mmpose, torch; assert torch.__version__.startswith('2.0.1')"

COPY start.sh \
    setup_models.py \
    worker.py \
    ltx_image_to_video.yaml \
    /app/runpod/
RUN chmod +x /app/runpod/start.sh

WORKDIR /app/runpod
EXPOSE 8000
CMD ["/app/runpod/start.sh"]
