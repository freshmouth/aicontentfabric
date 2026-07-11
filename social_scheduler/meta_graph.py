from __future__ import annotations

import json
import mimetypes
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MetaGraphError(RuntimeError):
    pass


@dataclass(frozen=True)
class MetaGraphConfig:
    graph_version: str = "v23.0"
    instagram_user_id: str = ""
    instagram_access_token_env: str = "INSTAGRAM_ACCESS_TOKEN"
    instagram_api_base: str = "auto"
    facebook_page_id: str = ""
    facebook_access_token_env: str = "FACEBOOK_PAGE_ACCESS_TOKEN"
    facebook_mode: str = "reels"
    poll_seconds: int = 8
    timeout_seconds: int = 900


def load_config(raw: dict[str, Any]) -> MetaGraphConfig:
    meta = dict(raw.get("meta_graph") or raw)
    return MetaGraphConfig(
        graph_version=str(meta.get("graph_version") or "v23.0").strip().lstrip("/"),
        instagram_user_id=str(meta.get("instagram_user_id") or "").strip(),
        instagram_access_token_env=str(meta.get("instagram_access_token_env") or "INSTAGRAM_ACCESS_TOKEN").strip(),
        instagram_api_base=str(meta.get("instagram_api_base") or "auto").strip(),
        facebook_page_id=str(meta.get("facebook_page_id") or "").strip(),
        facebook_access_token_env=str(meta.get("facebook_access_token_env") or "FACEBOOK_PAGE_ACCESS_TOKEN").strip(),
        facebook_mode=str(meta.get("facebook_mode") or "reels").strip().lower(),
        poll_seconds=int(meta.get("poll_seconds") or 8),
        timeout_seconds=int(meta.get("timeout_seconds") or 900),
    )


class MetaGraphClient:
    def __init__(self, config: MetaGraphConfig):
        self.config = config
        self.base_url = f"https://graph.facebook.com/{config.graph_version}"
        self.instagram_base_url = self.resolve_instagram_base_url()

    def resolve_instagram_base_url(self) -> str:
        configured = self.config.instagram_api_base.strip()
        if configured and configured.lower() != "auto":
            return configured.rstrip("/")
        token = os.environ.get(self.config.instagram_access_token_env, "").strip()
        if token.startswith("IGA"):
            return f"https://graph.instagram.com/{self.config.graph_version}"
        return self.base_url

    def publish_instagram_reel(self, *, video_url: str, caption: str, share_to_feed: bool = True) -> dict[str, Any]:
        if not self.config.instagram_user_id:
            raise MetaGraphError("instagram_user_id is required for Instagram publishing.")
        if not video_url.lower().startswith(("http://", "https://")):
            raise MetaGraphError("Instagram Reels publishing requires a public http(s) video_url.")
        token = env_secret(self.config.instagram_access_token_env)
        create = self.post(
            f"/{self.config.instagram_user_id}/media",
            token=token,
            base_url=self.instagram_base_url,
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "share_to_feed": "true" if share_to_feed else "false",
            },
        )
        creation_id = str(create.get("id") or "").strip()
        if not creation_id:
            raise MetaGraphError(f"Instagram media container response did not include id: {create}")
        status = self.wait_for_instagram_container(creation_id, token=token)
        publish = self.post(
            f"/{self.config.instagram_user_id}/media_publish",
            token=token,
            base_url=self.instagram_base_url,
            data={"creation_id": creation_id},
        )
        return {
            "platform": "instagram",
            "creation_id": creation_id,
            "status": status,
            "publish_response": publish,
            "post_id": publish.get("id"),
        }

    def wait_for_instagram_container(self, creation_id: str, *, token: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.get(
                f"/{creation_id}",
                token=token,
                params={"fields": "status_code,status"},
                base_url=self.instagram_base_url,
            )
            status_code = str(last.get("status_code") or "").upper()
            if status_code == "FINISHED":
                return last
            if status_code in {"ERROR", "EXPIRED"}:
                raise MetaGraphError(f"Instagram media container failed: {last}")
            time.sleep(max(2, self.config.poll_seconds))
        raise MetaGraphError(f"Timed out waiting for Instagram container {creation_id}. Last status: {last}")

    def publish_facebook_reel(self, *, video_path: Path, caption: str) -> dict[str, Any]:
        if not self.config.facebook_page_id:
            raise MetaGraphError("facebook_page_id is required for Facebook publishing.")
        video_path = Path(video_path)
        if not video_path.exists() or video_path.stat().st_size <= 0:
            raise MetaGraphError(f"Facebook video file is missing: {video_path}")
        token = env_secret(self.config.facebook_access_token_env)
        if self.config.facebook_mode == "page_video":
            return self.publish_facebook_page_video(video_path=video_path, caption=caption, token=token)
        start = self.post(
            f"/{self.config.facebook_page_id}/video_reels",
            token=token,
            data={"upload_phase": "start"},
        )
        video_id = str(start.get("video_id") or "").strip()
        upload_url = str(start.get("upload_url") or "").strip()
        if not video_id or not upload_url:
            raise MetaGraphError(f"Facebook Reels start response missing video_id/upload_url: {start}")
        upload = upload_binary_to_url(upload_url, video_path, token=token)
        finish = self.post(
            f"/{self.config.facebook_page_id}/video_reels",
            token=token,
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "description": caption,
                "published": "true",
            },
        )
        return {
            "platform": "facebook",
            "mode": "reels",
            "video_id": video_id,
            "upload_response": upload,
            "publish_response": finish,
            "post_id": finish.get("post_id") or finish.get("id") or video_id,
        }

    def publish_facebook_page_video(self, *, video_path: Path, caption: str, token: str) -> dict[str, Any]:
        response = self.post_multipart(
            f"/{self.config.facebook_page_id}/videos",
            token=token,
            fields={"description": caption, "published": "true"},
            files={"source": video_path},
        )
        return {
            "platform": "facebook",
            "mode": "page_video",
            "publish_response": response,
            "post_id": response.get("id"),
        }

    def get(
        self,
        path: str,
        *,
        token: str,
        params: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        query = dict(params or {})
        query["access_token"] = token
        url = (base_url or self.base_url) + path + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "ai-content-factory-meta-scheduler/1.0"})
        return open_json(request)

    def post(
        self,
        path: str,
        *,
        token: str,
        data: dict[str, str],
        base_url: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(data)
        payload["access_token"] = token
        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            (base_url or self.base_url) + path,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "ai-content-factory-meta-scheduler/1.0",
            },
        )
        return open_json(request)

    def post_multipart(self, path: str, *, token: str, fields: dict[str, str], files: dict[str, Path]) -> dict[str, Any]:
        boundary = f"----aiugc{int(time.time() * 1000)}"
        body = bytearray()
        all_fields = dict(fields)
        all_fields["access_token"] = token
        for name, value in all_fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for name, path_obj in files.items():
            path_obj = Path(path_obj)
            mime = mimetypes.guess_type(path_obj.name)[0] or "application/octet-stream"
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="{name}"; filename="{path_obj.name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n".encode()
            )
            body.extend(path_obj.read_bytes())
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            self.base_url + path,
            data=bytes(body),
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "ai-content-factory-meta-scheduler/1.0",
            },
        )
        return open_json(request, timeout=300)


def upload_binary_to_url(upload_url: str, video_path: Path, *, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        upload_url,
        data=video_path.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"OAuth {token}",
            "Content-Type": "application/octet-stream",
            "User-Agent": "ai-content-factory-meta-scheduler/1.0",
        },
    )
    return open_json(request, timeout=300)


def open_json(request: urllib.request.Request, timeout: int = 120) -> dict[str, Any]:
    context = None
    if os.environ.get("META_GRAPH_SSL_NO_VERIFY", "").strip().lower() in {"1", "true", "yes"}:
        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MetaGraphError(f"Meta Graph HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise MetaGraphError(f"Meta Graph request failed: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MetaGraphError(f"Meta Graph returned non-JSON response: {raw[:500]}") from exc


def env_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MetaGraphError(f"Missing required environment variable: {name}")
    return value
