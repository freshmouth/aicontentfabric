from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import runpod
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles


MODEL_ROOT = Path("/workspace/models")
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "/workspace/outputs"))
LTX2_MODEL_PATH = Path(os.getenv("LTX2_MODEL_PATH", "/workspace/models/ltx2"))
LTX2_REPO_PATH = Path(os.getenv("LTX2_REPO_PATH", "/opt/ltx2"))
LTX2_PYTHON = os.getenv("LTX2_PYTHON", "/opt/ltx2/.venv/bin/python")
FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_PATH", "ffprobe")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_INLINE_OUTPUT_BYTES = int(os.getenv("MAX_INLINE_OUTPUT_BYTES", str(40 * 1024 * 1024)))
GPU_LOCK = threading.Lock()

CHECKPOINT_FILE = os.getenv("LTX2_CHECKPOINT_FILE", "ltx-2.3-22b-distilled-1.1.safetensors")
SPATIAL_UPSCALER_FILE = os.getenv("LTX2_SPATIAL_UPSCALER_FILE", "ltx-2.3-spatial-upscaler-x2-1.1.safetensors")
DISTILLED_LORA_FILE = os.getenv("LTX2_DISTILLED_LORA_FILE", "ltx-2.3-22b-distilled-lora-384-1.1.safetensors")

app = FastAPI(title="LTX-2.3 Audio-to-Video Worker", version="1.0.0")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_ROOT)), name="outputs")


@app.get("/health")
def health() -> dict[str, Any]:
    return health_payload()


@app.post("/generate_ltx_audio_scene")
def generate_ltx_audio_scene_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return generate_ltx_audio_scene(unwrap_input(body), request_id=f"http-{uuid.uuid4().hex}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def runpod_handler(job: dict[str, Any]) -> dict[str, Any]:
    payload = unwrap_input(job.get("input") or {})
    action = str(payload.get("action") or payload.get("endpoint") or "").strip().lstrip("/")
    request_id = str(job.get("id") or uuid.uuid4().hex)
    if action == "health":
        return health_payload()
    if action in {"generate_ltx_audio_scene", "audio_to_video", ""}:
        return generate_ltx_audio_scene(payload, request_id=request_id)
    raise ValueError("Unknown action. Use health or generate_ltx_audio_scene.")


def generate_ltx_audio_scene(payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    require_models()
    image_value = required_string(payload, "image_uri")
    audio_value = required_string(payload, "audio_uri")
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        prompt = "A realistic UGC selfie talking-head video. Preserve the reference image identity and scene."
    scene_id = safe_name(str(payload.get("scene_id") or "scene"))
    output_resolution = parse_resolution(str(payload.get("resolution") or "1080x1920"))
    generation_resolution = parse_resolution(
        str(payload.get("generation_resolution") or os.getenv("LTX2_GENERATION_RESOLUTION", "1088x1920"))
    )
    frame_rate = float(payload.get("frame_rate") or os.getenv("LTX2_FRAME_RATE", "25"))
    image_strength = float(payload.get("image_reference_strength") or os.getenv("LTX2_IMAGE_REFERENCE_STRENGTH", "1.0"))
    seed = int(payload.get("seed") or stable_seed(request_id, scene_id))
    timeout = int(payload.get("timeout_seconds") or os.getenv("LTX2_TIMEOUT_SECONDS", "1800"))

    job_dir = make_job_dir(request_id, scene_id)
    source_image = job_dir / "source_image.png"
    source_audio = job_dir / "source_audio.mp3"
    raw_output = job_dir / f"{scene_id}_raw.mp4"
    output_path = job_dir / f"{scene_id}_ltx_audio.mp4"
    materialize_media(image_value, source_image)
    materialize_media(audio_value, source_audio)

    audio_duration = ffprobe_duration(source_audio)
    min_duration = float(payload.get("min_audio_duration_seconds") or 2.0)
    max_duration = float(payload.get("max_audio_duration_seconds") or 20.0)
    if audio_duration < min_duration or audio_duration > max_duration:
        raise ValueError(
            f"Audio duration must be between {min_duration:g}s and {max_duration:g}s; got {audio_duration:.3f}s."
        )

    frame_count = normalize_frame_count(audio_duration, frame_rate, int(payload.get("max_frames") or 257))
    negative_prompt = str(
        payload.get("negative_prompt")
        or os.getenv(
            "LTX2_NEGATIVE_PROMPT",
            "cartoon, CGI, waxy skin, bad teeth, deformed mouth, choppy lip sync, looking away, text overlays",
        )
    )
    command = [
        LTX2_PYTHON,
        "-m",
        "ltx_pipelines.a2vid_two_stage",
        "--checkpoint-path",
        str(LTX2_MODEL_PATH / CHECKPOINT_FILE),
        "--distilled-lora",
        str(LTX2_MODEL_PATH / DISTILLED_LORA_FILE),
        str(payload.get("distilled_lora_strength") or os.getenv("LTX2_DISTILLED_LORA_STRENGTH", "0.8")),
        "--spatial-upsampler-path",
        str(LTX2_MODEL_PATH / SPATIAL_UPSCALER_FILE),
        "--gemma-root",
        str(LTX2_MODEL_PATH / "gemma"),
        "--prompt",
        prompt,
        "--negative-prompt",
        negative_prompt,
        "--output-path",
        str(raw_output),
        "--height",
        str(generation_resolution[1]),
        "--width",
        str(generation_resolution[0]),
        "--num-frames",
        str(frame_count),
        "--frame-rate",
        f"{frame_rate:g}",
        "--image",
        str(source_image),
        "0",
        f"{image_strength:g}",
        "--audio-path",
        str(source_audio),
        "--audio-max-duration",
        f"{frame_count / frame_rate:.3f}",
        "--seed",
        str(seed),
        "--num-inference-steps",
        str(payload.get("num_inference_steps") or os.getenv("LTX2_NUM_INFERENCE_STEPS", "30")),
        "--a2v-guidance-scale",
        str(payload.get("a2v_guidance_scale") or os.getenv("LTX2_A2V_GUIDANCE_SCALE", "2.0")),
        "--video-cfg-guidance-scale",
        str(payload.get("video_cfg_guidance_scale") or os.getenv("LTX2_VIDEO_CFG_GUIDANCE_SCALE", "3.0")),
        "--max-batch-size",
        str(payload.get("max_batch_size") or os.getenv("LTX2_MAX_BATCH_SIZE", "1")),
    ]
    optional_arg(command, "--quantization", payload.get("quantization") or os.getenv("LTX2_QUANTIZATION", "fp8-cast"))
    optional_arg(command, "--offload", payload.get("offload") or os.getenv("LTX2_OFFLOAD", "none"))

    started = time.monotonic()
    with GPU_LOCK:
        run_command(command, cwd=LTX2_REPO_PATH, timeout=timeout, log_path=job_dir / "ltx_audio.log")
    normalize_video(raw_output, output_path, output_resolution, fps=int(round(frame_rate)))
    return package_output(
        output_path,
        request_id,
        {
            "status": "completed",
            "scene_id": scene_id,
            "generation_mode": "open_source_ltx_2_3_audio_to_video",
            "source_image_sha256": sha256_file(source_image),
            "source_audio_sha256": sha256_file(source_audio),
            "audio_duration": round(audio_duration, 3),
            "frame_rate": frame_rate,
            "num_frames": frame_count,
            "generation_resolution": f"{generation_resolution[0]}x{generation_resolution[1]}",
            "output_resolution": f"{output_resolution[0]}x{output_resolution[1]}",
            "generation_time_seconds": round(time.monotonic() - started, 3),
        },
    )


def health_payload() -> dict[str, Any]:
    files = [
        LTX2_MODEL_PATH / CHECKPOINT_FILE,
        LTX2_MODEL_PATH / SPATIAL_UPSCALER_FILE,
        LTX2_MODEL_PATH / DISTILLED_LORA_FILE,
    ]
    gemma_dir = LTX2_MODEL_PATH / "gemma"
    ready = all(path.is_file() and path.stat().st_size > 0 for path in files) and gemma_dir.is_dir()
    return {
        "status": "healthy" if ready else "models_missing",
        "worker_release": os.getenv("WORKER_RELEASE", "unknown"),
        "cuda_available": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "torch_version": torch.__version__,
        "cuda_version": torch.version.cuda,
        "ltx2_audio_ready": ready,
        "models_root": str(MODEL_ROOT),
        "outputs_root": str(OUTPUT_ROOT),
    }


def require_models() -> None:
    health = health_payload()
    if not health.get("ltx2_audio_ready"):
        raise RuntimeError(f"LTX-2.3 audio models are missing under {LTX2_MODEL_PATH}. Run setup_models.py first.")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is not available.")


def materialize_media(value: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if value.startswith(("http://", "https://")):
        request = urllib.request.Request(value, headers={"User-Agent": "ltx2-audio-worker/1.0"})
        with urllib.request.urlopen(request, timeout=180) as response:
            target.write_bytes(response.read())
    else:
        encoded = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
        target.write_bytes(base64.b64decode(encoded, validate=False))
    if target.stat().st_size <= 0:
        raise ValueError(f"Decoded input is empty: {target.name}")


def package_output(path: Path, request_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(f"Generated output is missing or empty: {path}")
    result = dict(metadata)
    result["output_path"] = str(path)
    result["size_bytes"] = path.stat().st_size
    if PUBLIC_BASE_URL:
        relative = path.relative_to(OUTPUT_ROOT).as_posix()
        result["video_url"] = f"{PUBLIC_BASE_URL}/outputs/{relative}"
        return result
    if path.stat().st_size > MAX_INLINE_OUTPUT_BYTES:
        raise RuntimeError(
            "Output is too large for inline base64. Set PUBLIC_BASE_URL or lower output size. "
            f"File: {path} ({path.stat().st_size} bytes)."
        )
    result["video_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
    return result


def normalize_video(input_path: Path, output_path: Path, resolution: tuple[int, int], fps: int) -> None:
    width, height = resolution
    run_command(
        [
            FFMPEG,
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps={fps}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(output_path),
        ],
        cwd=output_path.parent,
        timeout=300,
        log_path=output_path.with_suffix(".ffmpeg.log"),
    )


def run_command(args: list[str], cwd: Path, timeout: int, log_path: Path) -> None:
    started = time.monotonic()
    result = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    log_path.write_text(
        "$ " + subprocess.list2cmdline(args) + "\n"
        + f"exit={result.returncode} elapsed={time.monotonic() - started:.2f}s\n"
        + result.stdout + "\n" + result.stderr,
        encoding="utf-8",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"Command failed: {detail}. See {log_path}.")


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed for {path}")
    return float(result.stdout.strip())


def normalize_frame_count(duration: float, fps: float, max_frames: int) -> int:
    desired = max(9, math.ceil(duration * fps))
    frames = 9 + math.ceil((desired - 9) / 8) * 8
    if frames > max_frames:
        raise ValueError(
            f"Requested audio duration needs {frames} frames at {fps:g} fps, above max_frames={max_frames}. "
            "Use shorter scene audio or increase max_frames if the GPU has enough VRAM."
        )
    return frames


def parse_resolution(value: str) -> tuple[int, int]:
    parts = value.lower().replace(" ", "").split("x")
    if len(parts) != 2:
        raise ValueError(f"Resolution must be WIDTHxHEIGHT, got {value!r}")
    width, height = int(parts[0]), int(parts[1])
    if width <= 0 or height <= 0:
        raise ValueError(f"Resolution values must be positive, got {value!r}")
    return width, height


def optional_arg(command: list[str], flag: str, value: Any) -> None:
    if value in (None, "", "none", "None"):
        return
    command.extend([flag, str(value)])


def make_job_dir(request_id: str, scene_id: str) -> Path:
    directory = OUTPUT_ROOT / safe_name(request_id) / scene_id / "ltx2_audio"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def unwrap_input(body: dict[str, Any]) -> dict[str, Any]:
    nested = body.get("input")
    return dict(nested) if isinstance(nested, dict) else dict(body)


def required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required string field: {key}")
    return value.strip()


def safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)
    return cleaned[:96] or "job"


def stable_seed(*values: str) -> int:
    digest = hashlib.sha256("|".join(values).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    if os.getenv("RUNPOD_SERVERLESS", "1").lower() in {"1", "true", "yes", "on"}:
        runpod.serverless.start({"handler": runpod_handler})
    else:
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
