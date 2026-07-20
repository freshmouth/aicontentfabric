from __future__ import annotations

import base64
import json
import os
import random
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_REFERENCE_FOLDER = "catalog/sal-celtica/ugc-refs"


class CloudinaryReferenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudinaryResource:
    secure_url: str
    public_id: str
    width: int
    height: int


def load_cloudinary_references(
    *,
    folder: str,
    count: int,
    out_dir: Path,
) -> list[Path]:
    resources = list_reference_images(folder=folder, max_results=max(1, min(500, count * 20)))
    portrait = [item for item in resources if item.width > 0 and item.height / item.width >= 1.25]
    pool = portrait or resources
    if not pool:
        raise CloudinaryReferenceError(f"No Cloudinary reference images found in folder: {folder}")
    random.seed(folder)
    selected = pool if len(pool) <= count else random.sample(pool, count)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    manifest: list[dict[str, Any]] = []
    for index, resource in enumerate(selected, start=1):
        suffix = Path(urllib.parse.urlparse(resource.secure_url).path).suffix or ".jpg"
        output_path = out_dir / f"cloudinary_ref_{index:02d}{suffix}"
        download_url(resource.secure_url, output_path)
        paths.append(output_path)
        manifest.append(
            {
                "index": index,
                "public_id": resource.public_id,
                "secure_url": resource.secure_url,
                "width": resource.width,
                "height": resource.height,
                "local_path": str(output_path),
            }
        )
    (out_dir / "cloudinary_references.json").write_text(
        json.dumps({"folder": folder, "references": manifest}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return paths


def list_reference_images(*, folder: str, max_results: int) -> list[CloudinaryResource]:
    cloud_name, auth = cloudinary_auth()
    prefix_results = list_images(cloud_name, auth, folder, max_results)
    if prefix_results:
        return prefix_results
    return search_images(cloud_name, auth, f'folder="{folder}"', max_results)


def cloudinary_auth() -> tuple[str, str]:
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key = os.environ.get("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.environ.get("CLOUDINARY_API_SECRET", "").strip()
    if not api_key and os.environ.get("CLOUDINARY", "").strip():
        api_key = os.environ["CLOUDINARY"].strip()
    if not cloud_name or not api_key or not api_secret:
        raise CloudinaryReferenceError(
            "Cloudinary references require CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, "
            "and CLOUDINARY_API_SECRET. A raw API key alone is not enough for the Admin API."
        )
    token = base64.b64encode(f"{api_key}:{api_secret}".encode("utf-8")).decode("ascii")
    return cloud_name, token


def list_images(cloud_name: str, auth: str, prefix: str, max_results: int) -> list[CloudinaryResource]:
    params = {"max_results": str(max_results), "type": "upload"}
    if prefix:
        params["prefix"] = prefix
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/resources/image?{urllib.parse.urlencode(params)}"
    data = cloudinary_json(url, auth=auth)
    return parse_resources(data)


def search_images(cloud_name: str, auth: str, expression: str, max_results: int) -> list[CloudinaryResource]:
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/resources/search"
    payload = json.dumps({"expression": expression, "max_results": max_results}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    try:
        with open_url(request, timeout=60) as response:
            return parse_resources(json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CloudinaryReferenceError(f"Cloudinary search error HTTP {exc.code}: {detail[:400]}") from exc


def cloudinary_json(url: str, *, auth: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"Authorization": f"Basic {auth}"})
    try:
        with open_url(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CloudinaryReferenceError(f"Cloudinary list error HTTP {exc.code}: {detail[:400]}") from exc


def parse_resources(data: dict[str, Any]) -> list[CloudinaryResource]:
    resources = data.get("resources") if isinstance(data, dict) else []
    parsed: list[CloudinaryResource] = []
    for item in resources or []:
        if not isinstance(item, dict) or not item.get("secure_url"):
            continue
        parsed.append(
            CloudinaryResource(
                secure_url=str(item.get("secure_url")),
                public_id=str(item.get("public_id") or ""),
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
            )
        )
    return parsed


def download_url(url: str, output_path: Path) -> None:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "mix-v3-cloudinary-reference/1.0"})
    with open_url(request, timeout=120) as response:
        output_path.write_bytes(response.read())


def open_url(request: urllib.request.Request, *, timeout: int):
    if os.environ.get("CLOUDINARY_SSL_NO_VERIFY", "").strip().lower() in {"1", "true", "yes"}:
        return urllib.request.urlopen(request, timeout=timeout, context=ssl._create_unverified_context())
    return urllib.request.urlopen(request, timeout=timeout)
