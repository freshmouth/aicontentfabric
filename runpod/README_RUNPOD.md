# RunPod Deployment: LTX + MuseTalk V2

This directory is isolated from the existing local pipeline. It builds a GPU worker containing:

- LTX-Video image-to-video generation
- MuseTalk 1.5 lip sync
- Persistent models under `/workspace/models/`
- Persistent outputs under `/workspace/outputs/`
- FastAPI routes for Pod debugging
- RunPod Serverless action dispatch for scale-to-zero operation

Upstream projects:

- [Lightricks LTX-Video](https://github.com/Lightricks/LTX-Video)
- [Tencent Music MuseTalk](https://github.com/TMElyralab/MuseTalk)
- [RunPod Serverless documentation](https://docs.runpod.io/serverless/overview)

## 1. Build and push the image

From the repository root:

```bash
docker build -f runpod/Dockerfile -t YOUR_REGISTRY/v2-video-worker:latest runpod
docker push YOUR_REGISTRY/v2-video-worker:latest
```

### Cloud build when local Docker storage is constrained

The repository includes `.github/workflows/build-runpod.yml`. Push the repository to GitHub, open **Actions > Build RunPod V2 image**, and select **Run workflow**. GitHub builds and publishes:

```text
ghcr.io/YOUR_GITHUB_USERNAME/v2-video-worker:latest
```

No local Docker image or build cache is created. The workflow uses GitHub's built-in package token, so no registry secret is required. After the first build, make the package public in its GitHub Package settings, or configure GHCR credentials in the RunPod template for a private package.

The image installs LTX and MuseTalk into separate Python virtual environments because their current Transformers dependencies conflict. LTX uses the base PyTorch runtime; MuseTalk uses its supported PyTorch 2.0.1 CUDA 11.8 stack and the matching prebuilt MMCV wheel.

The included `ltx_image_to_video.yaml` uses the LTX 2B 0.9.8 distilled image-conditioning pipeline and disables automatic prompt enhancement. This avoids loading Florence and Llama solely to rewrite the V2 pipeline's existing scene prompts.

## 2. Create a Network Volume

In the RunPod console:

1. Open **Storage > Network Volumes**.
2. Create a volume large enough for both model families and cache. Start with at least 100 GB.
3. Choose the same region as the GPU endpoint.
4. Attach the volume to a temporary Pod or the Serverless endpoint.
5. Mount it at exactly `/workspace`.

The worker stores:

```text
/workspace/
  models/
    ltx/
    musetalk/
    huggingface/
  outputs/
```

The volume prevents model downloads from repeating when workers stop and restart.

## 3. Configure environment variables

Set these on the Pod or Serverless template:

```text
HF_TOKEN=optional_hugging_face_token
RUNPOD_API_KEY=your_runpod_api_key
LTX_MODEL_PATH=/workspace/models/ltx
MUSETALK_MODEL_PATH=/workspace/models/musetalk
HF_HOME=/workspace/models/huggingface
OUTPUT_ROOT=/workspace/outputs
RUNPOD_SERVERLESS=1
```

No secrets are embedded in the image. `HF_TOKEN` is passed only to Hugging Face downloads. Private/gated model access requires a token with permission.

Optional runtime settings:

```text
LTX_WIDTH=576
LTX_HEIGHT=1024
LTX_FPS=30
LTX_TIMEOUT_SECONDS=1200
MUSETALK_TIMEOUT_SECONDS=1200
MAX_INLINE_OUTPUT_BYTES=41943040
LTX_PIPELINE_CONFIG=/app/runpod/ltx_image_to_video.yaml
```

## 4. Download models once

Attach the Network Volume to a temporary GPU Pod using the built image. In its terminal run:

```bash
python /app/runpod/setup_models.py
```

`start.sh` also runs this command on startup, but completed files are detected and skipped. A volume lock prevents concurrent workers from downloading the same files.

The setup downloads:

- LTX 2B 0.9.8 distilled checkpoint and spatial upscaler
- LTX text encoder cache
- MuseTalk 1.5 UNet/config
- MuseTalk SD-VAE, Whisper, DWPose, SyncNet, face parser, and ResNet components

Verify the volume:

```bash
find /workspace/models/ltx -maxdepth 2 -type f -printf '%p %s bytes\n'
find /workspace/models/musetalk -maxdepth 3 -type f -printf '%p %s bytes\n'
cat /workspace/models/ltx/.setup_complete.json
cat /workspace/models/musetalk/.setup_complete.json
```

Stop the temporary Pod after verification. The models remain on the Network Volume.

## 5. Deploy Serverless for scale-to-zero billing

Create a RunPod Serverless endpoint using the image and attached Network Volume:

1. Set the volume mount to `/workspace`.
2. Set **Active workers / minimum workers** to `0`.
3. Set the maximum workers appropriate for the volume and GPU quota; start with `1`.
4. Use a GPU with enough VRAM for the selected LTX checkpoint.
5. Keep `RUNPOD_SERVERLESS=1`.
6. Configure an idle timeout so the worker scales down after jobs finish.

With minimum workers set to zero, GPU billing occurs only while a worker is starting or processing jobs. The persistent Network Volume continues to retain model files independently.

## 6. Serverless actions

RunPod exposes its standard `/run`, `/runsync`, and `/status/{job_id}` API. The worker dispatches these `input.action` values:

- `health`
- `generate_ltx_scene`
- `generate_musetalk_lipsync`

Health job:

```bash
curl -X POST "https://api.runpod.ai/v2/ENDPOINT_ID/runsync" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"health"}}'
```

LTX job body:

```json
{
  "input": {
    "action": "generate_ltx_scene",
    "scene_id": "scene_01",
    "image": "data:image/png;base64,...",
    "prompt": "Subtle UGC motion, direct eye contact, fixed camera",
    "duration": 5,
    "image_to_video_only": true,
    "preserve_identity": true,
    "preserve_object": true,
    "preserve_framing": true
  }
}
```

MuseTalk job body:

```json
{
  "input": {
    "action": "generate_musetalk_lipsync",
    "scene_id": "scene_01",
    "video": "data:video/mp4;base64,...",
    "audio": "data:audio/mpeg;base64,...",
    "preserve_identity": true,
    "preserve_eye_contact": true,
    "preserve_framing": true
  }
}
```

The response contains `video_base64` unless `PUBLIC_BASE_URL` is configured for Pod HTTP mode. All generated files are also retained under `/workspace/outputs/`.

## 7. Optional Pod HTTP mode

For interactive debugging, set:

```text
RUNPOD_SERVERLESS=0
PORT=8000
```

Expose port 8000. The worker provides:

```text
GET  /health
POST /generate_ltx_scene
POST /generate_musetalk_lipsync
GET  /outputs/...
```

Pod mode bills for the entire time the Pod is running. Use it only for setup and debugging, then stop the Pod.

## 8. Connect the local V2 adapters

For Serverless, both local adapters use the same endpoint ID and RunPod API key. Update the dedicated V2 config, not the original pipeline config:

```json
{
  "ltx": {
    "endpoint_url": "https://api.runpod.ai/v2/ENDPOINT_ID/run",
    "api_key_env": "RUNPOD_API_KEY",
    "status_url_template": "https://api.runpod.ai/v2/ENDPOINT_ID/status/{job_id}",
    "input": {"action": "generate_ltx_scene"}
  },
  "musetalk": {
    "endpoint_url": "https://api.runpod.ai/v2/ENDPOINT_ID/run",
    "api_key_env": "RUNPOD_API_KEY",
    "status_url_template": "https://api.runpod.ai/v2/ENDPOINT_ID/status/{job_id}",
    "input": {"action": "generate_musetalk_lipsync"}
  }
}
```

Set locally:

```powershell
$env:RUNPOD_API_KEY = "your-runpod-api-key"
```

## 9. Run the two-scene V2 test

After the health job reports both models ready:

```powershell
python pipeline_v2_open_source.py `
  --quantity 1 `
  --scenes 2 `
  --out v2_test_2scene `
  --config config.v2_open_source.example.json
```

Verify:

```powershell
Get-ChildItem v2_test_2scene\video_01\scene_01.png
Get-ChildItem v2_test_2scene\video_01\scene_01_audio.mp3
Get-ChildItem v2_test_2scene\video_01\scene_01_ltx.mp4
Get-ChildItem v2_test_2scene\video_01\scene_01_musetalk.mp4
Get-ChildItem v2_test_2scene\video_01\scene_02.png
Get-ChildItem v2_test_2scene\video_01\scene_02_audio.mp3
Get-ChildItem v2_test_2scene\video_01\scene_02_ltx.mp4
Get-ChildItem v2_test_2scene\video_01\scene_02_musetalk.mp4
Get-ChildItem v2_test_2scene\video_01\final_video_v2.mp4
```
