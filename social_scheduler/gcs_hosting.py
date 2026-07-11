from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GcsHostingError(RuntimeError):
    pass


@dataclass(frozen=True)
class GcsHostingConfig:
    enabled: bool = False
    bucket: str = ""
    prefix: str = "meta-reels"
    url_mode: str = "public"
    signed_url_duration: str = "2h"
    make_public: bool = False
    project_env: str = "GOOGLE_CLOUD_PROJECT"
    ssl_no_verify_env: str = "GOOGLE_OMNI_SSL_NO_VERIFY"


def load_gcs_config(raw: dict[str, Any]) -> GcsHostingConfig:
    cfg = dict(raw.get("google_cloud_storage") or raw.get("gcs_hosting") or {})
    return GcsHostingConfig(
        enabled=bool(cfg.get("enabled", False)),
        bucket=str(cfg.get("bucket") or os.environ.get("GCS_VIDEO_BUCKET", "")).strip(),
        prefix=str(cfg.get("prefix") or "meta-reels").strip().strip("/"),
        url_mode=str(cfg.get("url_mode") or "public").strip().lower(),
        signed_url_duration=str(cfg.get("signed_url_duration") or "2h").strip(),
        make_public=bool(cfg.get("make_public", False)),
        project_env=str(cfg.get("project_env") or "GOOGLE_CLOUD_PROJECT").strip(),
        ssl_no_verify_env=str(cfg.get("ssl_no_verify_env") or "GOOGLE_OMNI_SSL_NO_VERIFY").strip(),
    )


def ensure_public_video_url(video_path: Path, record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    if str(record.get("public_video_url") or "").strip():
        return None
    hosting = load_gcs_config(config)
    if not hosting.enabled:
        return None
    result = upload_video(video_path, hosting)
    record["public_video_url"] = result["public_video_url"]
    record["hosted_video"] = result
    return result


def upload_video(video_path: Path, config: GcsHostingConfig) -> dict[str, Any]:
    video_path = Path(video_path).resolve()
    if not video_path.exists() or video_path.stat().st_size <= 0:
        raise GcsHostingError(f"Video file is missing: {video_path}")
    if not config.bucket:
        raise GcsHostingError("GCS hosting is enabled but no bucket is configured.")
    object_name = build_object_name(video_path, config.prefix)
    gcs_uri = f"gs://{config.bucket}/{object_name}"
    run_gcloud(["gcloud.cmd", "storage", "cp", str(video_path), gcs_uri], config)
    if config.make_public:
        run_gcloud(["gcloud.cmd", "storage", "objects", "update", gcs_uri, "--add-acl-grant=entity=AllUsers,role=READER"], config)
    if config.url_mode == "signed":
        public_url = sign_url(gcs_uri, config)
    elif config.url_mode == "public":
        public_url = public_gcs_url(config.bucket, object_name)
    else:
        raise GcsHostingError(f"Unsupported GCS url_mode: {config.url_mode}")
    return {
        "provider": "google_cloud_storage",
        "bucket": config.bucket,
        "object_name": object_name,
        "gcs_uri": gcs_uri,
        "url_mode": config.url_mode,
        "public_video_url": public_url,
        "uploaded_at": int(time.time()),
    }


def build_object_name(video_path: Path, prefix: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_stem = "".join(ch.lower() if ch.isalnum() else "-" for ch in video_path.stem).strip("-")
    safe_stem = "-".join(part for part in safe_stem.split("-") if part)[:80] or "video"
    return f"{prefix}/{stamp}_{safe_stem}{video_path.suffix.lower()}"


def public_gcs_url(bucket: str, object_name: str) -> str:
    return f"https://storage.googleapis.com/{urllib.parse.quote(bucket)}/{urllib.parse.quote(object_name, safe='/')}"


def sign_url(gcs_uri: str, config: GcsHostingConfig) -> str:
    result = run_gcloud(
        ["gcloud.cmd", "storage", "sign-url", gcs_uri, f"--duration={config.signed_url_duration}", "--format=json"],
        config,
    )
    text = result.stdout.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GcsHostingError(f"Could not parse signed URL response: {text[:500]}") from exc
    if isinstance(payload, list) and payload:
        url = str(payload[0].get("signed_url") or payload[0].get("url") or "").strip()
    elif isinstance(payload, dict):
        url = str(payload.get("signed_url") or payload.get("url") or "").strip()
    else:
        url = ""
    if not url:
        raise GcsHostingError(f"Signed URL response did not contain a URL: {payload}")
    return url


def run_gcloud(command: list[str], config: GcsHostingConfig) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env.get(config.ssl_no_verify_env, "").strip().lower() in {"1", "true", "yes"}:
        env["CLOUDSDK_AUTH_DISABLE_SSL_VALIDATION"] = "True"
    result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False, env=env)
    if result.returncode != 0:
        raise GcsHostingError(
            "GCS command failed:\n"
            + " ".join(command)
            + "\nSTDOUT:\n"
            + result.stdout
            + "\nSTDERR:\n"
            + result.stderr
        )
    return result
