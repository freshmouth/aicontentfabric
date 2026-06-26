from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from filelock import FileLock
from huggingface_hub import hf_hub_download, snapshot_download


MODEL_ROOT = Path("/workspace/models")
HF_HOME = Path(os.getenv("HF_HOME", MODEL_ROOT / "huggingface"))
LTX2_MODEL_PATH = Path(os.getenv("LTX2_MODEL_PATH", MODEL_ROOT / "ltx2"))
HF_TOKEN = os.getenv("HF_TOKEN") or None

LTX2_REPO = os.getenv("LTX2_MODEL_REPO", "Lightricks/LTX-2.3")
LTX2_CHECKPOINT_FILE = os.getenv("LTX2_CHECKPOINT_FILE", "ltx-2.3-22b-distilled-1.1.safetensors")
LTX2_SPATIAL_UPSCALER_FILE = os.getenv(
    "LTX2_SPATIAL_UPSCALER_FILE",
    "ltx-2.3-spatial-upscaler-x2-1.1.safetensors",
)
LTX2_DISTILLED_LORA_FILE = os.getenv(
    "LTX2_DISTILLED_LORA_FILE",
    "ltx-2.3-22b-distilled-lora-384-1.1.safetensors",
)
GEMMA_REPO = os.getenv("GEMMA_MODEL_REPO", "google/gemma-3-12b-it-qat-q4_0-unquantized")


def main() -> int:
    ensure_under_model_root(LTX2_MODEL_PATH)
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    HF_HOME.mkdir(parents=True, exist_ok=True)
    LTX2_MODEL_PATH.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(MODEL_ROOT / ".ltx2_audio_setup.lock"), timeout=3600)
    with lock:
        setup_ltx2()
    print("LTX-2.3 audio model setup complete.")
    return 0


def setup_ltx2() -> None:
    files = [
        (LTX2_REPO, LTX2_CHECKPOINT_FILE, LTX2_CHECKPOINT_FILE),
        (LTX2_REPO, LTX2_SPATIAL_UPSCALER_FILE, LTX2_SPATIAL_UPSCALER_FILE),
        (LTX2_REPO, LTX2_DISTILLED_LORA_FILE, LTX2_DISTILLED_LORA_FILE),
    ]
    required = [LTX2_MODEL_PATH / target for _, _, target in files]
    gemma_dir = LTX2_MODEL_PATH / "gemma"
    marker = LTX2_MODEL_PATH / ".setup_complete.json"
    if all(valid_file(path) for path in required) and valid_dir(gemma_dir) and valid_file(marker):
        print(f"LTX-2.3 audio models already present at {LTX2_MODEL_PATH}; skipping download.")
        return

    for repo, filename, target in files:
        download_hf_file(repo, filename, LTX2_MODEL_PATH / target)

    if not valid_dir(gemma_dir):
        snapshot_download(
            repo_id=GEMMA_REPO,
            token=HF_TOKEN,
            cache_dir=str(HF_HOME),
            local_dir=str(gemma_dir),
            local_dir_use_symlinks=False,
        )

    verify_files(required, "LTX-2.3 audio")
    if not valid_dir(gemma_dir):
        raise RuntimeError(f"Gemma text encoder download is incomplete: {gemma_dir}")
    write_marker(
        marker,
        {
            "repo": LTX2_REPO,
            "checkpoint": LTX2_CHECKPOINT_FILE,
            "spatial_upscaler": LTX2_SPATIAL_UPSCALER_FILE,
            "distilled_lora": LTX2_DISTILLED_LORA_FILE,
            "gemma_repo": GEMMA_REPO,
        },
    )


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


def valid_dir(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


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
        print(f"LTX-2.3 audio model setup failed: {exc}", file=sys.stderr)
        raise
