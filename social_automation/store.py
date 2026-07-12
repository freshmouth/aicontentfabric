from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol


class DeliveryStore(Protocol):
    def claim(self, event_key: str, details: dict[str, Any]) -> bool: ...
    def complete(self, event_key: str, details: dict[str, Any]) -> None: ...
    def fail(self, event_key: str, error: str, details: dict[str, Any]) -> None: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryDeliveryStore:
    def __init__(self):
        self.records: dict[str, dict[str, Any]] = {}

    def claim(self, event_key: str, details: dict[str, Any]) -> bool:
        existing = self.records.get(event_key)
        if existing and existing.get("status") in {"processing", "delivered"}:
            return False
        attempts = int((existing or {}).get("attempts", 0)) + 1
        self.records[event_key] = {**details, "status": "processing", "attempts": attempts, "updated_at": utc_now()}
        return True

    def complete(self, event_key: str, details: dict[str, Any]) -> None:
        self.records[event_key] = {**self.records.get(event_key, {}), **details, "status": "delivered", "updated_at": utc_now()}

    def fail(self, event_key: str, error: str, details: dict[str, Any]) -> None:
        self.records[event_key] = {**self.records.get(event_key, {}), **details, "status": "failed", "error": error, "updated_at": utc_now()}


class JsonDeliveryStore(MemoryDeliveryStore):
    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        self.lock = threading.Lock()
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.records = dict(raw.get("records") or {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps({"records": self.records}, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def claim(self, event_key: str, details: dict[str, Any]) -> bool:
        with self.lock:
            claimed = super().claim(event_key, details)
            if claimed:
                self._save()
            return claimed

    def complete(self, event_key: str, details: dict[str, Any]) -> None:
        with self.lock:
            super().complete(event_key, details)
            self._save()

    def fail(self, event_key: str, error: str, details: dict[str, Any]) -> None:
        with self.lock:
            super().fail(event_key, error, details)
            self._save()


class FirestoreDeliveryStore:
    def __init__(self, collection: str = "meta_comment_deliveries"):
        try:
            from google.cloud import firestore
        except ImportError as exc:
            raise RuntimeError("google-cloud-firestore is required for Firestore storage.") from exc
        self.client = firestore.Client()
        self.firestore = firestore
        self.collection = self.client.collection(collection)

    def _ref(self, event_key: str):
        document_id = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
        return self.collection.document(document_id)

    def claim(self, event_key: str, details: dict[str, Any]) -> bool:
        ref = self._ref(event_key)
        transaction = self.client.transaction()
        now = datetime.now(timezone.utc)

        @self.firestore.transactional
        def claim_in_transaction(transaction, document_ref):
            snapshot = document_ref.get(transaction=transaction)
            prior = snapshot.to_dict() if snapshot.exists else {}
            if prior.get("status") == "delivered":
                return False
            if prior.get("status") == "processing":
                lease = prior.get("lease_expires_at")
                if isinstance(lease, datetime) and lease > now:
                    return False
            transaction.set(
                document_ref,
                {
                    **details,
                    "event_key": event_key,
                    "status": "processing",
                    "attempts": int(prior.get("attempts", 0)) + 1,
                    "lease_expires_at": now + timedelta(minutes=5),
                    "updated_at": utc_now(),
                },
            )
            return True

        return bool(claim_in_transaction(transaction, ref))

    def complete(self, event_key: str, details: dict[str, Any]) -> None:
        self._ref(event_key).set({**details, "status": "delivered", "updated_at": utc_now()}, merge=True)

    def fail(self, event_key: str, error: str, details: dict[str, Any]) -> None:
        self._ref(event_key).set({**details, "status": "failed", "error": error, "updated_at": utc_now()}, merge=True)
