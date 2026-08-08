from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any


class VideoStorageError(RuntimeError):
    pass


class VideoStorage:
    def __init__(self, settings) -> None:
        self.settings = settings
        self._client = None

    def metadata(self, uri: str) -> dict[str, Any] | None:
        bucket_name, object_name = split_gcs_uri(uri)
        blob = self._storage_client().bucket(bucket_name).blob(object_name)
        if not blob.exists():
            return None
        blob.reload()
        return {
            "size_bytes": int(blob.size or 0),
            "md5_hash": str(blob.md5_hash or ""),
            "generation": int(blob.generation or 0),
            "content_type": str(blob.content_type or "video/mp4"),
            "updated": blob.updated.isoformat() if blob.updated else None,
        }

    def byte_range(self, uri: str, range_header: str | None) -> tuple[Iterator[bytes], dict[str, Any]]:
        bucket_name, object_name = split_gcs_uri(uri)
        blob = self._storage_client().bucket(bucket_name).blob(object_name)
        blob.reload()
        total = int(blob.size or 0)
        if total <= 0:
            raise VideoStorageError("The archived video is empty.")
        start, end, partial = parse_range(range_header, total)
        generation = int(blob.generation or 0)

        def chunks() -> Iterator[bytes]:
            cursor = start
            chunk_size = 1024 * 1024
            while cursor <= end:
                chunk_end = min(end, cursor + chunk_size - 1)
                yield blob.download_as_bytes(
                    start=cursor,
                    end=chunk_end,
                    if_generation_match=generation,
                )
                cursor = chunk_end + 1

        return chunks(), {
            "status_code": 206 if partial else 200,
            "content_type": str(blob.content_type or "video/mp4"),
            "content_length": end - start + 1,
            "content_range": f"bytes {start}-{end}/{total}" if partial else None,
        }

    def _storage_client(self):
        if self._client is None:
            try:
                from google.cloud import storage
            except ImportError as exc:
                raise VideoStorageError("Video previews require google-cloud-storage.") from exc
            self._client = storage.Client(project=self.settings.google_cloud_project or None)
        return self._client


def split_gcs_uri(uri: str) -> tuple[str, str]:
    value = str(uri or "").removeprefix("gs://")
    bucket, separator, object_name = value.partition("/")
    if not separator or not bucket or not object_name:
        raise VideoStorageError("Invalid archived video URI.")
    return bucket, object_name


def parse_range(value: str | None, total: int) -> tuple[int, int, bool]:
    if not value:
        return 0, total - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match:
        raise VideoStorageError("Invalid video byte range.")
    left, right = match.groups()
    if not left and not right:
        raise VideoStorageError("Invalid video byte range.")
    if not left:
        length = min(total, int(right))
        return total - length, total - 1, True
    start = int(left)
    end = min(total - 1, int(right) if right else total - 1)
    if start >= total or start > end:
        raise VideoStorageError("Requested video range is outside the file.")
    return start, end, True
