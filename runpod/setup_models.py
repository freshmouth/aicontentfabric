from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

import gdown
from filelock import FileLock
from huggingface_hub import hf_hub_download, snapshot_download


MODEL_ROOT = Path("/workspace/models")
HF_HOME = Path(os.getenv("HF_HOME", MODEL_ROOT / "huggingface"))
LTX_MODEL_PATH = Path(os.getenv("LTX_MODEL_PATH", MODEL_ROOT / "ltx"))
MUSETALK_MODEL_PATH = Path(os.getenv("MUSETALK_MODEL_PATH", MODEL_ROOT / "musetalk"))
HF_TOKEN = os.getenv("HF_TOKEN") or None

LTX_REPO = os.getenv("LTX_MODEL_REPO", "Lightricks/LTX-Video")
LTX_CHECKPOINT = os.getenv("LTX_CHECKPOINT_FILE", "ltxv-2b-0.9.8-distilled.safetensors")
LTX_UPSCALER = os.getenv("LTX_UPSCALER_FILE", "ltxv-spatial-upscaler-0.9.8.safetensors")
LTX_TEXT_ENCODER_REPO = os.getenv("LTX_TEXT_ENCODER_REPO", "PixArt-alpha/PixArt-XL-2-1024-MS")


def main() -> int:
    ensure_under_model_root(LTX_MODEL_PATH)
    ensure_under_model_root(MUSETALK_MODEL_PATH)
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    HF_HOME.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(MODEL_ROOT / ".setup_models.lock"), timeout=3600)
    with lock:
        setup_ltx()
        setup_musetalk()
    print("Model setup complete.")
    return 0


def setup_ltx() -> None:
    LTX_MODEL_PATH.mkdir(parents=True, exist_ok=True)
    required = [LTX_MODEL_PATH / LTX_CHECKPOINT, LTX_MODEL_PATH / LTX_UPSCALER]
    if all(valid_file(path) for path in required) and valid_file(LTX_MODEL_PATH / ".setup_complete.json"):
        print(f"LTX models already present at {LTX_MODEL_PATH}; skipping download.")
        return

    download_hf_file(LTX_REPO, LTX_CHECKPOINT, LTX_MODEL_PATH / LTX_CHECKPOINT)
    download_hf_file(LTX_REPO, LTX_UPSCALER, LTX_MODEL_PATH / LTX_UPSCALER)

    # Pre-warm the persistent Hugging Face cache used by the LTX text encoder.
    snapshot_download(
        repo_id=LTX_TEXT_ENCODER_REPO,
        token=HF_TOKEN,
        cache_dir=str(HF_HOME),
    )
    write_marker(
        LTX_MODEL_PATH / ".setup_complete.json",
        {"repo": LTX_REPO, "files": [path.name for path in required], "text_encoder": LTX_TEXT_ENCODER_REPO},
    )
    verify_files(required, "LTX")


def setup_musetalk() -> None:
    MUSETALK_MODEL_PATH.mkdir(parents=True, exist_ok=True)
    files = [
        ("TMElyralab/MuseTalk", "musetalkV15/musetalk.json", "musetalkV15/musetalk.json"),
        ("TMElyralab/MuseTalk", "musetalkV15/unet.pth", "musetalkV15/unet.pth"),
        ("stabilityai/sd-vae-ft-mse", "config.json", "sd-vae/config.json"),
        ("stabilityai/sd-vae-ft-mse", "diffusion_pytorch_model.bin", "sd-vae/diffusion_pytorch_model.bin"),
        ("openai/whisper-tiny", "config.json", "whisper/config.json"),
        ("openai/whisper-tiny", "pytorch_model.bin", "whisper/pytorch_model.bin"),
        ("openai/whisper-tiny", "preprocessor_config.json", "whisper/preprocessor_config.json"),
        ("yzd-v/DWPose", "dw-ll_ucoco_384.pth", "dwpose/dw-ll_ucoco_384.pth"),
        ("ByteDance/LatentSync", "latentsync_syncnet.pt", "syncnet/latentsync_syncnet.pt"),
    ]
    required = [MUSETALK_MODEL_PATH / target for _, _, target in files]
    required.extend(
        [
            MUSETALK_MODEL_PATH / "face-parse-bisent/79999_iter.pth",
            MUSETALK_MODEL_PATH / "face-parse-bisent/resnet18-5c106cde.pth",
        ]
    )
    marker = MUSETALK_MODEL_PATH / ".setup_complete.json"
    if all(valid_file(path) for path in required) and valid_file(marker):
        print(f"MuseTalk models already present at {MUSETALK_MODEL_PATH}; skipping download.")
        return

    for repo, filename, target in files:
        download_hf_file(repo, filename, MUSETALK_MODEL_PATH / target)

    face_parse = MUSETALK_MODEL_PATH / "face-parse-bisent/79999_iter.pth"
    if not valid_file(face_parse):
        face_parse.parent.mkdir(parents=True, exist_ok=True)
        gdown.download(id="154JgKpzCPW82qINcVieuPH3fZ2e0P812", output=str(face_parse), quiet=False)

    resnet = MUSETALK_MODEL_PATH / "face-parse-bisent/resnet18-5c106cde.pth"
    if not valid_file(resnet):
        resnet.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(
            "https://download.pytorch.org/models/resnet18-5c106cde.pth",
            resnet,
        )

    verify_files(required, "MuseTalk")
    write_marker(marker, {"version": "1.5", "files": [str(path.relative_to(MUSETALK_MODEL_PATH)) for path in required]})


def download_hf_file(repo_id: str, filename: str, target: Path) -> None:
    if valid_file(target):
        print(f"Present: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    cached = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        token=HF_TOKEN,
        cache_dir=str(HF_HOME),
    )
    shutil.copy2(cached, target)
    if not valid_file(target):
        raise RuntimeError(f"Downloaded file is missing or empty: {target}")


def verify_files(paths: list[Path], label: str) -> None:
    missing = [str(path) for path in paths if not valid_file(path)]
    if missing:
        raise RuntimeError(f"{label} model setup incomplete. Missing: {', '.join(missing)}")


def valid_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def write_marker(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def ensure_under_model_root(path: Path) -> None:
    root = MODEL_ROOT.resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Model path must be under {MODEL_ROOT}: {path}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Model setup failed: {exc}", file=sys.stderr)
        raise
