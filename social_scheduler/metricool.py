from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class MetricoolError(RuntimeError):
    pass


@dataclass(frozen=True)
class MetricoolConfig:
    base_url: str = "https://app.metricool.com/api"
    user_id: str = ""
    blog_id: str = ""
    api_token_env: str = "METRICOOL_API_TOKEN"
    timezone: str = "UTC"
    networks: dict[str, str] = field(default_factory=lambda: {"instagram": "instagram", "facebook": "facebook"})
    post_types: dict[str, str] = field(default_factory=lambda: {"instagram": "REEL", "facebook": "REEL"})
    auto_publish: bool = True
    draft: bool = False
    show_reel_on_feed: bool = True
    save_external_media_files: bool = True
    video_cover_milliseconds: int = 0
    upload_resource_type: str = "planner"
    upload_chunk_size: int = 8 * 1024 * 1024
    timeout_seconds: int = 300


def load_config(raw: dict[str, Any]) -> MetricoolConfig:
    metricool = dict(raw.get("metricool") or raw)
    chunk_mb = int(metricool.get("upload_chunk_size_mb") or 8)
    return MetricoolConfig(
        base_url=str(metricool.get("base_url") or "https://app.metricool.com/api").strip().rstrip("/"),
        user_id=resolve_config_identifier(metricool, "user_id"),
        blog_id=resolve_config_identifier(metricool, "blog_id"),
        api_token_env=str(metricool.get("api_token_env") or "METRICOOL_API_TOKEN").strip(),
        timezone=str(metricool.get("timezone") or "UTC").strip(),
        networks={**{"instagram": "instagram", "facebook": "facebook"}, **dict(metricool.get("networks") or {})},
        post_types={**{"instagram": "REEL", "facebook": "REEL"}, **dict(metricool.get("post_types") or {})},
        auto_publish=bool(metricool.get("auto_publish", True)),
        draft=bool(metricool.get("draft", False)),
        show_reel_on_feed=bool(metricool.get("show_reel_on_feed", True)),
        save_external_media_files=bool(metricool.get("save_external_media_files", True)),
        video_cover_milliseconds=int(metricool.get("video_cover_milliseconds") or 0),
        upload_resource_type=str(metricool.get("upload_resource_type") or "planner").strip(),
        upload_chunk_size=max(5 * 1024 * 1024, chunk_mb * 1024 * 1024),
        timeout_seconds=int(metricool.get("timeout_seconds") or 300),
    )


def resolve_config_identifier(config: dict[str, Any], field: str) -> str:
    explicit_env = str(config.get(f"{field}_env") or "").strip()
    if explicit_env:
        return os.environ.get(explicit_env, "").strip()
    value = str(config.get(field) or "").strip()
    if value and value.upper() == value and value.replace("_", "").isalnum():
        env_value = os.environ.get(value, "").strip()
        if env_value:
            return env_value
    return value


class MetricoolClient:
    def __init__(self, config: MetricoolConfig):
        self.config = config

    def schedule_reel(
        self,
        *,
        video_path: Path,
        caption: str,
        platforms: list[str],
        publish_at: str,
        first_comment: str = "",
        job_id: str = "",
    ) -> dict[str, Any]:
        video_path = Path(video_path)
        if not video_path.exists() or video_path.stat().st_size <= 0:
            raise MetricoolError(f"Video does not exist: {video_path}")
        media_url = self.upload_media(video_path)
        payload = self.build_scheduled_post_payload(
            caption=caption,
            media_url=media_url,
            platforms=platforms,
            publish_at=publish_at,
            first_comment=first_comment,
        )
        response = self.api_json(
            "POST",
            "/v2/scheduler/posts",
            params={"jobId": job_id} if job_id else None,
            data=payload,
        )
        data = dict(response.get("data") or {})
        return {
            "provider": "metricool",
            "platform": "metricool",
            "status": "scheduled",
            "metricool_post_id": data.get("id"),
            "metricool_post_uuid": data.get("uuid"),
            "media_url": media_url,
            "payload": redact_metricool_payload(payload),
            "response": response,
        }

    def build_scheduled_post_payload(
        self,
        *,
        caption: str,
        media_url: str,
        platforms: list[str],
        publish_at: str,
        first_comment: str = "",
    ) -> dict[str, Any]:
        providers = []
        for platform in platforms:
            network = self.config.networks.get(platform, platform)
            providers.append({"network": network})
        payload: dict[str, Any] = {
            "publicationDate": metricool_publication_date(publish_at, self.config.timezone),
            "text": caption,
            "providers": providers,
            "media": [media_url],
            "autoPublish": self.config.auto_publish,
            "saveExternalMediaFiles": self.config.save_external_media_files,
            "draft": self.config.draft,
        }
        if first_comment.strip():
            payload["firstCommentText"] = first_comment.strip()
        if self.config.video_cover_milliseconds > 0:
            payload["videoCoverMilliseconds"] = self.config.video_cover_milliseconds
        if "instagram" in platforms:
            payload["instagramData"] = {
                "autoPublish": self.config.auto_publish,
                "type": self.config.post_types.get("instagram", "REEL"),
                "showReelOnFeed": self.config.show_reel_on_feed,
            }
        if "facebook" in platforms:
            payload["facebookData"] = {
                "type": self.config.post_types.get("facebook", "REEL"),
            }
        return payload

    def upload_media(self, video_path: Path) -> str:
        content_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
        transaction = self.create_upload_transaction(video_path, content_type=content_type)
        data = dict(transaction.get("data") or {})
        upload_type = str(data.get("uploadType") or "").upper()
        if upload_type == "SIMPLE":
            file_url = self.upload_simple(video_path, data, content_type=content_type)
        elif upload_type == "MULTIPART":
            file_url = self.upload_multipart(video_path, data, content_type=content_type)
        else:
            raise MetricoolError(f"Metricool upload transaction returned unknown uploadType: {data}")
        if not file_url:
            raise MetricoolError(f"Metricool upload did not return a media URL: {data}")
        return file_url

    def create_upload_transaction(self, video_path: Path, *, content_type: str) -> dict[str, Any]:
        parts = build_upload_parts(video_path, self.config.upload_chunk_size)
        return self.api_json(
            "PUT",
            "/v2/media/s3/upload-transactions",
            data={
                "resourceType": self.config.upload_resource_type,
                "contentType": content_type,
                "fileExtension": video_path.suffix.lstrip(".") or "mp4",
                "parts": parts,
            },
        )

    def upload_simple(self, video_path: Path, transaction: dict[str, Any], *, content_type: str) -> str:
        presigned_url = str(transaction.get("presignedUrl") or "").strip()
        file_url = str(transaction.get("fileUrl") or "").strip()
        if not presigned_url or not file_url:
            raise MetricoolError(f"Metricool simple upload response missing presignedUrl/fileUrl: {transaction}")
        self.raw_put(presigned_url, video_path.read_bytes(), content_type=content_type)
        complete = self.api_json("PATCH", "/v2/media/s3/upload-transactions", data={"simple": {"fileUrl": file_url}})
        completed = dict(complete.get("data") or {})
        return str(completed.get("convertedFileUrl") or completed.get("fileUrl") or file_url)

    def upload_multipart(self, video_path: Path, transaction: dict[str, Any], *, content_type: str) -> str:
        upload_id = str(transaction.get("uploadId") or "").strip()
        key = str(transaction.get("key") or "").strip()
        if not upload_id or not key:
            raise MetricoolError(f"Metricool multipart upload response missing uploadId/key: {transaction}")
        completed_parts = []
        with video_path.open("rb") as handle:
            for part in transaction.get("parts") or []:
                part_number = int(part["partNumber"])
                start = int(part["startByte"])
                end = int(part["endByte"])
                url = str(part["presignedUrl"])
                handle.seek(start)
                body = handle.read(end - start)
                checksum = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
                headers = self.raw_put(
                    url,
                    body,
                    content_type=content_type,
                    extra_headers={"x-amz-checksum-sha256": checksum},
                )
                etag = headers.get("ETag") or headers.get("etag") or ""
                if not etag:
                    raise MetricoolError(f"S3 multipart upload part {part_number} did not return an ETag.")
                completed_parts.append({"partNumber": part_number, "etag": etag})
        complete = self.api_json(
            "PATCH",
            "/v2/media/s3/upload-transactions",
            data={"multipart": {"uploadId": upload_id, "key": key, "parts": completed_parts}},
        )
        completed = dict(complete.get("data") or {})
        return str(completed.get("convertedFileUrl") or completed.get("fileUrl") or transaction.get("fileUrl") or "")

    def api_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = env_secret(self.config.api_token_env)
        query = {"userId": self.config.user_id, "blogId": self.config.blog_id}
        if not query["userId"]:
            raise MetricoolError("metricool.user_id is required.")
        if not query["blogId"]:
            raise MetricoolError("metricool.blog_id is required.")
        query.update({k: v for k, v in (params or {}).items() if v is not None and v != ""})
        url = self.config.base_url + path + "?" + urllib.parse.urlencode(query)
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "X-Mc-Auth": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ai-content-factory-metricool-scheduler/1.0",
            },
        )
        return open_json(request, timeout=self.config.timeout_seconds)

    def raw_put(
        self,
        url: str,
        body: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": content_type,
            "User-Agent": "ai-content-factory-metricool-scheduler/1.0",
        }
        headers.update({key: value for key, value in (extra_headers or {}).items() if value})
        request = urllib.request.Request(
            url,
            data=body,
            method="PUT",
            headers=headers,
        )
        context = ssl_context()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds, context=context) as response:
                response.read()
                return dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise MetricoolError(f"Metricool S3 upload HTTP {exc.code}: {body_text}") from exc
        except urllib.error.URLError as exc:
            raise MetricoolError(f"Metricool S3 upload failed: {exc}") from exc


def build_upload_parts(path: Path, chunk_size: int) -> list[dict[str, Any]]:
    parts = []
    start = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            end = start + len(chunk)
            parts.append(
                {
                    "size": len(chunk),
                    "startByte": start,
                    "endByte": end,
                    "hash": base64.b64encode(hashlib.sha256(chunk).digest()).decode("ascii"),
                }
            )
            start = end
    if not parts:
        raise MetricoolError(f"Cannot upload empty file: {path}")
    return parts


def metricool_publication_date(value: str, timezone_name: str) -> dict[str, str]:
    tz = resolve_timezone(timezone_name or "UTC")
    parsed = parse_datetime(value)
    local = parsed.astimezone(tz).replace(microsecond=0)
    return {"dateTime": local.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": timezone_name or "UTC"}


def resolve_timezone(timezone_name: str) -> timezone:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        fixed_offsets = {
            "UTC": timezone.utc,
            "America/Mexico_City": timezone(timedelta(hours=-6)),
        }
        if timezone_name in fixed_offsets:
            return fixed_offsets[timezone_name]
        raise MetricoolError(
            f"Timezone data is not available for {timezone_name}. "
            "Install the Python tzdata package or use an ISO publish time with UTC."
        )


def parse_datetime(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw or raw.lower() == "now":
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def open_json(request: urllib.request.Request, timeout: int = 120) -> dict[str, Any]:
    context = ssl_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MetricoolError(f"Metricool HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise MetricoolError(f"Metricool request failed: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MetricoolError(f"Metricool returned non-JSON response: {raw[:500]}") from exc


def ssl_context() -> ssl.SSLContext | None:
    if os.environ.get("METRICOOL_SSL_NO_VERIFY", "").strip().lower() in {"1", "true", "yes"}:
        return ssl._create_unverified_context()
    return None


def env_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MetricoolError(f"Missing required environment variable: {name}")
    return value


def redact_metricool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(payload))
    if "media" in redacted:
        redacted["media"] = [redact_url(str(item)) for item in redacted.get("media") or []]
    return redacted


def redact_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
