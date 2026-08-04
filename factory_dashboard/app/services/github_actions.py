from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import truststore


class GitHubActionsError(RuntimeError):
    pass


class GitHubActions:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.base = f"https://api.github.com/repos/{settings.github_owner}/{settings.github_repo}"

    def dispatch(
        self,
        *,
        request_id: str,
        account_id: str,
        payload: dict[str, Any],
        publish_at: str | None,
        dry_run: bool,
        skip_publish: bool,
    ) -> dict[str, Any]:
        if not self.settings.github_token:
            raise GitHubActionsError("GITHUB_DASHBOARD_TOKEN is not configured.")
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
        if len(encoded) > 55000:
            raise GitHubActionsError("Creative request is too large for GitHub workflow dispatch.")
        body = {
            "ref": self.settings.github_branch,
            "inputs": {
                "account": account_id,
                "force": "true",
                "dry_run": "true" if dry_run else "false",
                "plan_only": "false",
                "publish_at": publish_at or "",
                "request_id": request_id,
                "request_payload_b64": encoded,
                "skip_publish": "true" if skip_publish else "false",
            },
        }
        self._request(
            f"{self.base}/actions/workflows/{urllib.parse.quote(self.settings.github_workflow)}/dispatches",
            method="POST",
            body=body,
        )
        return {"status": "queued", "request_id": request_id}

    def find_run(self, request_id: str) -> dict[str, Any] | None:
        data = self._request(
            f"{self.base}/actions/workflows/{urllib.parse.quote(self.settings.github_workflow)}/runs"
            f"?event=workflow_dispatch&branch={urllib.parse.quote(self.settings.github_branch)}&per_page=30"
        )
        marker = request_id.lower()
        for run in data.get("workflow_runs", []):
            title = str(run.get("display_title") or run.get("name") or "").lower()
            if marker in title:
                return {
                    "github_run_id": run.get("id"),
                    "github_run_url": run.get("html_url"),
                    "status": normalize_run_status(run.get("status"), run.get("conclusion")),
                    "error_code": str(run.get("conclusion") or "") if run.get("conclusion") == "failure" else None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
        return None

    def _request(self, url: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=60,
                context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            ) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise GitHubActionsError(f"GitHub API failed: HTTP {exc.code}: {detail}") from exc


def normalize_run_status(status: str | None, conclusion: str | None) -> str:
    if status != "completed":
        return "in_progress" if status == "in_progress" else "queued"
    if conclusion == "success":
        return "succeeded"
    if conclusion == "cancelled":
        return "cancelled"
    return "failed"
