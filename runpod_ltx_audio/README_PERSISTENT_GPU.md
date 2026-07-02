# Persistent GPU Path for LTX-2.3 Audio-to-Video

This is the practical proof path. Use it before trying Serverless again.

The current working production pipeline remains unchanged. This path only validates the isolated open-source LTX-2.3 audio-to-video worker.

## Why This Path

LTX-2.3 audio-to-video is too heavy for the local GTX 950M. Prove it first on one persistent high-VRAM GPU where models stay on disk and the worker stays warm. After the model stack is known-good, it can be converted back to Serverless if needed.

Use:

- A100 80GB, H100, H200, or L40S
- 200GB or larger persistent volume mounted at `/workspace`
- Port `8000` exposed

## RunPod Pod Setup

Create a regular GPU Pod, not a Serverless endpoint.

Recommended settings:

```text
GPU: A100 80GB or H100 first; L40S can be tried after the proof works
Container image: ghcr.io/freshmouth/ltx2-audio-worker:ae52b490649cf1d6fd8e911c5bd212e8f84ec332
Volume mount path: /workspace
Expose HTTP port: 8000
Container disk: 40GB+
Volume: 200GB+
```

Environment variables:

```text
RUNPOD_SERVERLESS=0
HF_TOKEN=your_huggingface_token
MODEL_ROOT=/workspace/models
HF_HOME=/workspace/models/huggingface
LTX2_MODEL_PATH=/workspace/models/ltx2
OUTPUT_ROOT=/workspace/outputs
MAX_INLINE_OUTPUT_BYTES=160000000
LTX2_GENERATION_RESOLUTION=1088x1920
LTX2_FRAME_RATE=25
LTX2_NUM_INFERENCE_STEPS=30
LTX2_A2V_GUIDANCE_SCALE=2.0
LTX2_VIDEO_CFG_GUIDANCE_SCALE=3.0
LTX2_QUANTIZATION=fp8-cast
LTX2_OFFLOAD=none
```

If the pod gives you a stable public HTTP URL, set:

```text
PUBLIC_BASE_URL=https://YOUR_POD_URL
```

That lets the worker return a video URL instead of a large base64 JSON response.

### Optional REST Helper

This repo includes a dry-run-first helper:

```powershell
$env:HF_TOKEN = "hf_your_token"
$env:RUNPOD_LTX_NETWORK_VOLUME_ID = "your_network_volume_id"
.\runpod_ltx_audio\create_persistent_pod.ps1
```

If the dry-run JSON looks right and you are ready to start paid GPU time:

```powershell
.\runpod_ltx_audio\create_persistent_pod.ps1 -Create
```

For the existing US-TX-3 network volume, pass the data center explicitly:

```powershell
.\runpod_ltx_audio\create_persistent_pod.ps1 -DataCenterIds US-TX-3 -Create
```

## Verify the Worker

Once the pod is running, open:

```text
https://YOUR_POD_URL/health
```

Healthy response should include:

```json
{
  "status": "healthy",
  "cuda_available": true,
  "ltx2_audio_ready": true
}
```

The first boot will download models to `/workspace/models`. Later boots should skip downloads.

## Local Smoke Test

On this Windows project machine, set the pod generate URL:

```powershell
$env:LTX_AUDIO_FASTAPI_GENERATE_URL = "https://YOUR_POD_URL/generate_ltx_audio_scene"
```

Then run a one-scene proof using already generated Claire assets and audio:

```powershell
python run_ltx_audio_persistent_smoke.py --scenes 1 --out ltx_audio_persistent_smoke
```

If that succeeds, run two scenes:

```powershell
python run_ltx_audio_persistent_smoke.py --scenes 2 --out ltx_audio_persistent_smoke_2
```

Expected outputs:

```text
ltx_audio_persistent_smoke_2/
  audio.mp3
  scene_01.png
  scene_01_audio.mp3
  scene_01_ltx_audio.mp4
  scene_02.png
  scene_02_audio.mp3
  scene_02_ltx_audio.mp4
  final_video_v2.mp4
  provider_log_persistent_ltx_audio.json
```

## Full Pipeline Test

After the smoke test works:

```powershell
$env:LTX_AUDIO_FASTAPI_GENERATE_URL = "https://YOUR_POD_URL/generate_ltx_audio_scene"
python pipeline_v2_open_source.py --quantity 1 --scenes 2 --out project_v2_ltx_persistent --config config.v2_ltx_2_3_audio_persistent.example.json
```

This still uses:

```text
OpenAI Images
ElevenLabs
Local Whisper
LTX-2.3 open-source audio-to-video
FFmpeg hard-cut assembly
```

MuseTalk is not used.

## Stop Cost

When not testing, stop the pod. The persistent volume keeps the downloaded models, so the next run should not re-download them.
