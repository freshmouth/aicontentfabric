from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any


ALLOWED_IMAGE_TYPES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class AttachmentStorageError(RuntimeError):
    pass


class AttachmentStorage:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.local_root = settings.root / "factory_dashboard" / ".data" / "uploads"
        self._client = None

    def put(
        self,
        *,
        account_id: str,
        attachment_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> str:
        validate_image(content_type, data)
        safe_name = sanitize_filename(filename, content_type)
        object_name = f"{self.settings.upload_prefix}/{account_id}/{attachment_id}/{safe_name}"
        if self.settings.upload_bucket:
            blob = self._storage_client().bucket(self.settings.upload_bucket).blob(object_name)
            blob.upload_from_string(data, content_type=content_type)
            return f"gs://{self.settings.upload_bucket}/{object_name}"
        path = (self.local_root / account_id / attachment_id / safe_name).resolve()
        path.relative_to(self.local_root.resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def data_url(self, attachment: dict[str, Any]) -> str:
        content_type = str(attachment.get("content_type") or "")
        uri = str(attachment.get("storage_uri") or "")
        if uri.startswith("gs://"):
            bucket_name, object_name = split_gcs_uri(uri)
            data = self._storage_client().bucket(bucket_name).blob(object_name).download_as_bytes()
        else:
            path = Path(uri).resolve()
            path.relative_to(self.local_root.resolve())
            data = path.read_bytes()
        validate_image(content_type, data)
        return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"

    def _storage_client(self):
        if self._client is None:
            try:
                from google.cloud import storage
            except ImportError as exc:
                raise AttachmentStorageError("Cloud uploads require google-cloud-storage.") from exc
            self._client = storage.Client(project=self.settings.google_cloud_project or None)
        return self._client


def validate_image(content_type: str, data: bytes) -> None:
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise AttachmentStorageError("Only JPEG, PNG, and WebP photos are supported.")
    if not data:
        raise AttachmentStorageError("The uploaded photo is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentStorageError("Each photo must be 8 MB or smaller.")
    signatures = ALLOWED_IMAGE_TYPES[content_type]
    if not any(data.startswith(signature) for signature in signatures):
        raise AttachmentStorageError("The uploaded file does not match its image type.")
    if content_type == "image/webp" and data[8:12] != b"WEBP":
        raise AttachmentStorageError("The uploaded WebP file is invalid.")


def sanitize_filename(filename: str, content_type: str) -> str:
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
    stem = Path(filename or "reference").stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:80] or "reference"
    return f"{stem}{extension}"


def split_gcs_uri(uri: str) -> tuple[str, str]:
    value = uri.removeprefix("gs://")
    bucket, separator, object_name = value.partition("/")
    if not separator or not bucket or not object_name:
        raise AttachmentStorageError("Invalid cloud attachment URI.")
    return bucket, object_name
