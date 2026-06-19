from __future__ import annotations

import base64
import hashlib
import json
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
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles


MODEL_ROOT = Path("/workspace/models")
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "/workspace/outputs"))
LTX_MODEL_PATH = Path(os.getenv("LTX_MODEL_PATH", "/workspace/models/ltx"))
MUSETALK_MODEL_PATH = Path(os.getenv("MUSETALK_MODEL_PATH", "/workspace/models/musetalk"))
LTX_REPO_PATH = Path(os.getenv("LTX_REPO_PATH", "/opt/ltx-video"))
MUSETALK_REPO_PATH = Path(os.getenv("MUSETALK_REPO_PATH", "/opt/musetalk"))
LTX_PYTHON = os.getenv("LTX_PYTHON", "/opt/venvs/ltx/bin/python")
MUSETALK_PYTHON = os.getenv("MUSETALK_PYTHON", "/opt/venvs/musetalk/bin/python")
FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_INLINE_OUTPUT_BYTES = int(os.getenv("MAX_INLINE_OUTPUT_BYTES", str(40 * 1024 * 1024)))
GPU_LOCK = threading.Lock()

app = FastAPI(title="V2 Open Source Video Worker", version="1.0.0")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_ROOT)), name="outputs")


@app.get("/health")
def health() -> dict[str, Any]:
    return health_payload()


@app.post("/generate_ltx_scene")
def generate_ltx_scene_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return generate_ltx_scene(unwrap_input(body), request_id=f"http-{uuid.uuid4().hex}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/generate_musetalk_lipsync")
def generate_musetalk_lipsync_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return generate_musetalk_lipsync(unwrap_input(body), request_id=f"http-{uuid.uuid4().hex}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def runpod_handler(job: dict[str, Any]) -> dict[str, Any]:
    payload = unwrap_input(job.get("input") or {})
    action = str(payload.get("action") or payload.get("endpoint") or "").strip().lstrip("/")
    request_id = str(job.get("id") or uuid.uuid4().hex)
    if action == "health":
        return health_payload()
    if action == "generate_ltx_scene":
        return generate_ltx_scene(payload, request_id=request_id)
    if action == "generate_musetalk_lipsync":
        return generate_musetalk_lipsync(payload, request_id=request_id)
    raise ValueError(
        "Unknown action. Use health, generate_ltx_scene, or generate_musetalk_lipsync."
    )


def generate_ltx_scene(payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    require_models("ltx")
    image_value = required_string(payload, "image")
    prompt = required_string(payload, "prompt")
    scene_id = safe_name(str(payload.get("scene_id") or "scene"))
    duration = clamp(float(payload.get("duration") or 5.0), 1.0, 10.0)
    if payload.get("image_to_video_only") is not True:
        raise ValueError("LTX worker accepts image-to-video requests only.")

    job_dir = make_job_dir(request_id, scene_id, "ltx")
    image_path = job_dir / "source.png"
    materialize_media(image_value, image_path)
    if not image_path.exists() or image_path.stat().st_size <= 0:
        raise ValueError("A non-empty source image is required for LTX image-to-video.")

    fps = int(os.getenv("LTX_FPS", "30"))
    width = int(os.getenv("LTX_WIDTH", "576"))
    height = int(os.getenv("LTX_HEIGHT", "1024"))
    frames = ltx_frame_count(duration, fps)
    seed = int(payload.get("seed") or stable_seed(request_id, scene_id))
    raw_dir = job_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    pipeline_config = os.getenv(
        "LTX_PIPELINE_CONFIG",
        "/app/runpod/ltx_image_to_video.yaml",
    )
    command = [
        LTX_PYTHON,
        str(LTX_REPO_PATH / "inference.py"),
        "--prompt",
        prompt,
        "--conditioning_media_paths",
        str(image_path),
        "--conditioning_start_frames",
        "0",
        "--conditioning_strengths",
        "1.0",
        "--height",
        str(height),
        "--width",
        str(width),
        "--num_frames",
        str(frames),
        "--frame_rate",
        str(fps),
        "--seed",
        str(seed),
        "--pipeline_config",
        pipeline_config,
        "--output_path",
        str(raw_dir),
    ]

    started = time.monotonic()
    with GPU_LOCK:
        run_command(command, cwd=LTX_MODEL_PATH, timeout=int(os.getenv("LTX_TIMEOUT_SECONDS", "1200")), log_path=job_dir / "ltx.log")
    raw_video = newest_mp4(raw_dir)
    output_path = job_dir / f"{scene_id}_ltx.mp4"
    normalize_video(raw_video, output_path, fps=30)
    return package_output(
        output_path,
        request_id,
        {
            "status": "completed",
            "scene_id": scene_id,
            "generation_mode": "image_to_video",
            "source_image_sha256": sha256_file(image_path),
            "generation_time_seconds": round(time.monotonic() - started, 3),
        },
    )


def generate_musetalk_lipsync(payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    require_models("musetalk")
    video_value = required_string(payload, "video")
    audio_value = required_string(payload, "audio")
    scene_id = safe_name(str(payload.get("scene_id") or "scene"))
    job_dir = make_job_dir(request_id, scene_id, "musetalk")
    source_video = job_dir / "source.mp4"
    source_audio = job_dir / "source_audio.mp3"
    materialize_media(video_value, source_video)
    materialize_media(audio_value, source_audio)

    prepared_video = job_dir / "source_25fps.mp4"
    prepared_audio = job_dir / "source_audio.wav"
    run_command(
        [FFMPEG, "-nostdin", "-y", "-i", str(source_video), "-an", "-r", "25", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(prepared_video)],
        cwd=job_dir,
        timeout=180,
        log_path=job_dir / "prepare_video.log",
    )
    run_command(
        [FFMPEG, "-nostdin", "-y", "-i", str(source_audio), "-ac", "1", "-ar", "16000", str(prepared_audio)],
        cwd=job_dir,
        timeout=120,
        log_path=job_dir / "prepare_audio.log",
    )

    result_name = f"{scene_id}_musetalk.mp4"
    inference_config = job_dir / "musetalk.yaml"
    inference_config.write_text(
        yaml.safe_dump(
            {
                "task_0": {
                    "video_path": str(prepared_video),
                    "audio_path": str(prepared_audio),
                    "result_name": result_name,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    result_dir = job_dir / "results"
    ffmpeg_executable = shutil.which(FFMPEG) or FFMPEG
    ffmpeg_directory = str(Path(ffmpeg_executable).resolve().parent)
    command = [
        MUSETALK_PYTHON,
        "-m",
        "scripts.inference",
        "--inference_config",
        str(inference_config),
        "--result_dir",
        str(result_dir),
        "--unet_model_path",
        str(MUSETALK_MODEL_PATH / "musetalkV15/unet.pth"),
        "--unet_config",
        str(MUSETALK_MODEL_PATH / "musetalkV15/musetalk.json"),
        "--whisper_dir",
        str(MUSETALK_MODEL_PATH / "whisper"),
        "--version",
        "v15",
        "--ffmpeg_path",
        ffmpeg_directory,
        "--use_float16",
    ]
    started = time.monotonic()
    with GPU_LOCK:
        run_command(command, cwd=MUSETALK_REPO_PATH, timeout=int(os.getenv("MUSETALK_TIMEOUT_SECONDS", "1200")), log_path=job_dir / "musetalk.log")
    generated = result_dir / "v15" / result_name
    if not generated.exists():
        generated = newest_mp4(result_dir)
    output_path = job_dir / result_name
    normalize_video(generated, output_path, fps=30)
    return package_output(
        output_path,
        request_id,
        {
            "status": "completed",
            "scene_id": scene_id,
            "preserve_identity": bool(payload.get("preserve_identity", True)),
            "preserve_eye_contact": bool(payload.get("preserve_eye_contact", True)),
            "preserve_framing": bool(payload.get("preserve_framing", True)),
            "generation_time_seconds": round(time.monotonic() - started, 3),
        },
    )


def health_payload() -> dict[str, Any]:
    ltx_files = [
        LTX_MODEL_PATH / os.getenv("LTX_CHECKPOINT_FILE", "ltxv-2b-0.9.8-distilled.safetensors"),
        LTX_MODEL_PATH / os.getenv("LTX_UPSCALER_FILE", "ltxv-spatial-upscaler-0.9.8.safetensors"),
    ]
    musetalk_files = [
        MUSETALK_MODEL_PATH / "musetalkV15/unet.pth",
        MUSETALK_MODEL_PATH / "musetalkV15/musetalk.json",
        MUSETALK_MODEL_PATH / "whisper/pytorch_model.bin",
    ]
    return {
        "status": "healthy" if all(path.is_file() for path in ltx_files + musetalk_files) else "models_missing",
        "cuda_available": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "ltx_ready": all(path.is_file() and path.stat().st_size > 0 for path in ltx_files),
        "musetalk_ready": all(path.is_file() and path.stat().st_size > 0 for path in musetalk_files),
        "models_root": str(MODEL_ROOT),
        "outputs_root": str(OUTPUT_ROOT),
        "runpod_api_key_configured": bool(os.getenv("RUNPOD_API_KEY")),
    }


def require_models(provider: str) -> None:
    health = health_payload()
    key = f"{provider}_ready"
    if not health.get(key):
        raise RuntimeError(f"{provider} models are missing under {MODEL_ROOT}. Run setup_models.py first.")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is not available.")


def materialize_media(value: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if value.startswith(("http://", "https://")):
        request = urllib.request.Request(value, headers={"User-Agent": "v2-runpod-worker/1.0"})
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


def normalize_video(input_path: Path, output_path: Path, fps: int) -> None:
    run_command(
        [
            FFMPEG,
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps={fps}",
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


def newest_mp4(root: Path) -> Path:
    matches = sorted(root.rglob("*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not matches:
        raise RuntimeError(f"Inference completed without an MP4 under {root}.")
    return matches[0]


def make_job_dir(request_id: str, scene_id: str, stage: str) -> Path:
    directory = OUTPUT_ROOT / safe_name(request_id) / scene_id / stage
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


def ltx_frame_count(duration: float, fps: int) -> int:
    desired = max(9, min(257, round(duration * fps)))
    return max(9, min(257, round((desired - 1) / 8) * 8 + 1))


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


if __name__ == "__main__":
    if os.getenv("RUNPOD_SERVERLESS", "1").lower() in {"1", "true", "yes", "on"}:
        runpod.serverless.start({"handler": runpod_handler})
    else:
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
