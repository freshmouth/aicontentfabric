# RunPod LTX-2.3 Audio-to-Video Worker

This is a separate experimental worker for the V2 natural lip-sync path. It does not replace the existing Kling, LTX image-to-video, InfiniteTalk, or MuseTalk workers.

## Flow

```text
scene_XX.png
+ scene_XX_audio.mp3
-> open-source LTX-2.3 A2VidPipelineTwoStage
-> scene_XX_ltx_audio.mp4
```

The local V2 pipeline then assembles all `scene_XX_ltx_audio.mp4` files with FFmpeg hard cuts.

## What It Uses

- Official Lightricks `LTX-2` repository
- `A2VidPipelineTwoStage`
- LTX-2.3 distilled checkpoint
- LTX-2.3 spatial upscaler
- LTX-2.3 distilled LoRA
- Gemma text encoder

Official references:

- https://github.com/Lightricks/LTX-2
- https://docs.ltx.video/open-source-model/integration-tools/pytorch-api
- https://docs.ltx.video/open-source-model/getting-started/overview

## Persistent Paths

Use a RunPod Network Volume and mount it at:

```text
/workspace
```

Models are stored in:

```text
/workspace/models/ltx2
/workspace/models/huggingface
```

Outputs are stored in:

```text
/workspace/outputs
```

## Required Environment Variables

```text
HF_TOKEN
RUNPOD_API_KEY
```

`HF_TOKEN` is needed because Gemma may require Hugging Face access acceptance.

Optional:

```text
RUNPOD_SERVERLESS=1
LTX2_MODEL_PATH=/workspace/models/ltx2
LTX2_FRAME_RATE=25
LTX2_GENERATION_RESOLUTION=1088x1920
LTX2_NUM_INFERENCE_STEPS=30
LTX2_A2V_GUIDANCE_SCALE=2.0
LTX2_VIDEO_CFG_GUIDANCE_SCALE=3.0
LTX2_QUANTIZATION=fp8-cast
LTX2_OFFLOAD=none
```

The worker generates internally at `1088x1920` by default because the open-source two-stage pipeline expects dimensions divisible by 64. It normalizes the output to `1080x1920` before returning the MP4.

## Endpoints

FastAPI mode:

```text
GET /health
POST /generate_ltx_audio_scene
```

RunPod serverless mode:

```json
{
  "input": {
    "action": "generate_ltx_audio_scene",
    "image_uri": "data:image/png;base64,...",
    "audio_uri": "data:audio/mpeg;base64,...",
    "prompt": "Claire Natural speaking to camera...",
    "resolution": "1080x1920",
    "scene_id": "scene_01"
  }
}
```

## Build

Build and push this worker separately from the existing worker:

```powershell
docker build -t ghcr.io/YOUR_ORG/ltx2-audio-worker:latest .\runpod_ltx_audio
docker push ghcr.io/YOUR_ORG/ltx2-audio-worker:latest
```

For production, pin the `LTX2_GIT_REF` build arg to a tested commit instead of `main`.

## Local V2 Config

Use:

```json
{
  "video_provider": "ltx_2_3_audio",
  "lipsync_provider": "none",
  "ltx_2_3_audio": {
    "endpoint_url": "RUNPOD_LTX_AUDIO_ENDPOINT_URL",
    "status_url_template": "RUNPOD_LTX_AUDIO_STATUS_URL_TEMPLATE",
    "api_key_env": "RUNPOD_API_KEY",
    "input_wrapper": "input",
    "input": {
      "action": "generate_ltx_audio_scene"
    }
  }
}
```

For a serverless endpoint:

```powershell
$env:RUNPOD_LTX_AUDIO_ENDPOINT_URL = "https://api.runpod.ai/v2/YOUR_ENDPOINT_ID/run"
$env:RUNPOD_LTX_AUDIO_STATUS_URL_TEMPLATE = "https://api.runpod.ai/v2/YOUR_ENDPOINT_ID/status/{job_id}"
```

Then run:

```powershell
python pipeline_v2_open_source.py --quantity 1 --scenes 2 --out project_v2_ltx_audio_runpod --config config.v2_ltx_2_3_audio_runpod.example.json
```
