from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class MetaDeliveryError(RuntimeError):
    pass


class MetaDeliveryClient:
    def __init__(
        self,
        *,
        graph_version: str,
        instagram_user_id: str,
        facebook_page_id: str,
        instagram_token_env: str = "INSTAGRAM_ACCESS_TOKEN",
        facebook_token_env: str = "FACEBOOK_PAGE_ACCESS_TOKEN",
    ):
        self.graph_version = graph_version.strip().lstrip("/")
        self.instagram_user_id = instagram_user_id
        self.facebook_page_id = facebook_page_id
        self.instagram_token_env = instagram_token_env
        self.facebook_token_env = facebook_token_env

    def private_reply(self, platform: str, comment_id: str, message: str) -> dict[str, Any]:
        if platform == "instagram":
            token = self._token(self.instagram_token_env)
            base = self._instagram_base(token)
            return self._request_json(
                f"{base}/{self.instagram_user_id}/messages",
                token,
                {"recipient": {"comment_id": comment_id}, "message": {"text": message}},
            )
        if platform == "facebook":
            token = self._token(self.facebook_token_env)
            return self._request_form(
                f"https://graph.facebook.com/{self.graph_version}/{comment_id}/private_replies",
                token,
                {"message": message},
            )
        raise MetaDeliveryError(f"Unsupported platform: {platform}")

    def public_reply(self, platform: str, comment_id: str, message: str) -> dict[str, Any]:
        if not message:
            return {"skipped": True}
        if platform == "instagram":
            token = self._token(self.instagram_token_env)
            base = self._instagram_base(token)
            return self._request_form(f"{base}/{comment_id}/replies", token, {"message": message})
        if platform == "facebook":
            token = self._token(self.facebook_token_env)
            return self._request_form(
                f"https://graph.facebook.com/{self.graph_version}/{comment_id}/comments",
                token,
                {"message": message},
            )
        raise MetaDeliveryError(f"Unsupported platform: {platform}")

    def _instagram_base(self, token: str) -> str:
        host = "graph.instagram.com" if token.startswith("IGA") else "graph.facebook.com"
        return f"https://{host}/{self.graph_version}"

    @staticmethod
    def _token(name: str) -> str:
        token = os.environ.get(name, "").strip()
        if not token:
            raise MetaDeliveryError(f"Missing required environment variable: {name}")
        return token

    def _request_json(self, url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        return self._send(url, body, token, "application/json")

    def _request_form(self, url: str, token: str, payload: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        return self._send(url, body, token, "application/x-www-form-urlencoded")

    def _send(self, url: str, body: bytes, token: str, content_type: str) -> dict[str, Any]:
        last_error = ""
        for attempt in range(3):
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type,
                    "User-Agent": "ai-content-factory-comment-automation/1.0",
                },
            )
            try:
                context = ssl._create_unverified_context() if os.environ.get("META_GRAPH_SSL_NO_VERIFY") == "1" else None
                with urllib.request.urlopen(request, timeout=60, context=context) as response:
                    raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                response_body = exc.read().decode("utf-8", errors="replace")
                last_error = f"HTTP {exc.code}: {response_body}"
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
            except urllib.error.URLError as exc:
                last_error = str(exc)
            if attempt < 2:
                time.sleep(2**attempt)
        raise MetaDeliveryError(f"Meta delivery failed: {last_error}")

