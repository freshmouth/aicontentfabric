from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def issue_session(secret: str, *, lifetime_seconds: int) -> str:
    payload = {
        "exp": int(time.time()) + int(lifetime_seconds),
        "purpose": "factory-dashboard",
        "v": 1,
    }
    encoded = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _sign(secret, encoded)
    return f"{encoded}.{signature}"


def valid_session(value: str, secret: str) -> bool:
    try:
        encoded, supplied_signature = value.split(".", 1)
        if not hmac.compare_digest(supplied_signature, _sign(secret, encoded)):
            return False
        payload: dict[str, Any] = json.loads(_decode(encoded))
        return (
            payload.get("purpose") == "factory-dashboard"
            and int(payload.get("v") or 0) == 1
            and int(payload.get("exp") or 0) >= int(time.time())
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def _sign(secret: str, encoded: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return _encode(digest)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8")
