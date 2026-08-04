from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class Store(ABC):
    @abstractmethod
    def list(self, collection: str, *, account_id: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get(self, collection: str, record_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    def put(self, collection: str, record_id: str, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class LocalJsonStore(Store):
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def list(self, collection: str, *, account_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._read().get(collection, {}).values())
        if account_id:
            rows = [row for row in rows if row.get("account_id") == account_id]
        return sorted(rows, key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)

    def get(self, collection: str, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._read().get(collection, {}).get(record_id)
        return dict(value) if isinstance(value, dict) else None

    def put(self, collection: str, record_id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            database = self._read()
            database.setdefault(collection, {})[record_id] = data
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(database, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        return dict(data)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"drafts": {}, "jobs": {}, "account_overrides": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}


class FirestoreStore(Store):
    def __init__(self, project: str, prefix: str) -> None:
        try:
            from google.cloud import firestore
        except ImportError as exc:
            raise RuntimeError("Firestore storage requires google-cloud-firestore.") from exc
        self.client = firestore.Client(project=project or None)
        self.prefix = prefix

    def _collection(self, name: str):
        return self.client.collection(f"{self.prefix}_{name}")

    def list(self, collection: str, *, account_id: str | None = None) -> list[dict[str, Any]]:
        query = self._collection(collection)
        if account_id:
            query = query.where("account_id", "==", account_id)
        rows = [document.to_dict() for document in query.stream()]
        return sorted(rows, key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)

    def get(self, collection: str, record_id: str) -> dict[str, Any] | None:
        document = self._collection(collection).document(record_id).get()
        return document.to_dict() if document.exists else None

    def put(self, collection: str, record_id: str, data: dict[str, Any]) -> dict[str, Any]:
        self._collection(collection).document(record_id).set(data)
        return dict(data)


def build_store(settings) -> Store:
    if settings.store_backend == "firestore":
        return FirestoreStore(settings.google_cloud_project, settings.firestore_collection_prefix)
    return LocalJsonStore(settings.local_data_path)
