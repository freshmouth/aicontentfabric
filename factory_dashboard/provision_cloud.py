from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import truststore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT = "ai-content-factory-501821"
DEFAULT_REGION = "us-central1"
RUNTIME_SERVICE_ACCOUNT = "daily-factory@ai-content-factory-501821.iam.gserviceaccount.com"
REQUIRED_SERVICES = (
    "artifactregistry.googleapis.com",
    "firestore.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
)
RUNTIME_ROLES = (
    "roles/artifactregistry.writer",
    "roles/datastore.user",
    "roles/iam.serviceAccountUser",
    "roles/run.admin",
    "roles/secretmanager.secretAccessor",
    "roles/cloudscheduler.admin",
)


class ProvisionError(RuntimeError):
    pass


class GoogleRest:
    def __init__(self, token: str) -> None:
        self.token = token
        self.context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        *,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=90, context=self.context) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="replace")[:1600]
            raise ProvisionError(f"Google API {method} {url} failed: HTTP {exc.code}: {detail}") from exc


def gcloud_access_token() -> str:
    candidates = [
        Path(r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"),
        Path("gcloud.cmd"),
        Path("gcloud"),
    ]
    executable = next((str(path) for path in candidates if path.exists()), str(candidates[-1]))
    result = subprocess.run(
        [executable, "auth", "print-access-token"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    token = result.stdout.strip()
    if result.returncode or not token:
        raise ProvisionError("Could not obtain the active gcloud access token.")
    return token


def load_env_value(path: Path, name: str) -> str:
    if not path.exists():
        return ""
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def github_credential() -> str:
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    values = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    return str(values.get("password") or "").strip()


def wait_operation(api: GoogleRest, operation: dict[str, Any] | None, *, timeout: int = 180) -> None:
    name = str((operation or {}).get("name") or "")
    if not name:
        return
    if name.endswith("noop.DONE_OPERATION"):
        return
    url = name if name.startswith("https://") else f"https://serviceusage.googleapis.com/v1/{name}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = api.request("GET", url) or {}
        if status.get("done"):
            if status.get("error"):
                raise ProvisionError(f"Google operation failed: {status['error']}")
            return
        time.sleep(3)
    raise ProvisionError(f"Timed out waiting for operation: {name}")


def enable_services(api: GoogleRest, project: str) -> None:
    for service in REQUIRED_SERVICES:
        operation = api.request(
            "POST",
            f"https://serviceusage.googleapis.com/v1/projects/{project}/services/{service}:enable",
            {},
        )
        wait_operation(api, operation)
        print(f"service ready: {service}")


def ensure_repository(api: GoogleRest, project: str, region: str) -> None:
    base = f"https://artifactregistry.googleapis.com/v1/projects/{project}/locations/{region}/repositories"
    current = api.request("GET", f"{base}/ai-content-factory", allow_404=True)
    if current is None:
        operation = api.request(
            "POST",
            f"{base}?repositoryId=ai-content-factory",
            {"format": "DOCKER", "description": "AI Content Factory cloud images"},
        )
        wait_generic_operation(api, operation)
    print("artifact repository ready: ai-content-factory")


def ensure_firestore(api: GoogleRest, project: str, region: str) -> None:
    database = urllib.parse.quote("(default)", safe="()")
    base = f"https://firestore.googleapis.com/v1/projects/{project}/databases"
    current = api.request("GET", f"{base}/{database}", allow_404=True)
    if current is None:
        operation = api.request(
            "POST",
            f"{base}?databaseId={urllib.parse.quote('(default)')}",
            {"locationId": region, "type": "FIRESTORE_NATIVE"},
        )
        wait_generic_operation(api, operation, timeout=300)
    print("firestore ready: (default)")


def wait_generic_operation(api: GoogleRest, operation: dict[str, Any] | None, *, timeout: int = 180) -> None:
    name = str((operation or {}).get("name") or "")
    if not name:
        return
    if name.startswith("projects/"):
        if "/locations/" in name and "/operations/" in name:
            service = "artifactregistry.googleapis.com" if "repositories" not in name else "artifactregistry.googleapis.com"
            url = f"https://{service}/v1/{name}"
        else:
            url = f"https://firestore.googleapis.com/v1/{name}"
    else:
        url = name
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = api.request("GET", url) or {}
        if status.get("done"):
            if status.get("error"):
                raise ProvisionError(f"Google operation failed: {status['error']}")
            return
        time.sleep(3)
    raise ProvisionError(f"Timed out waiting for operation: {name}")


def ensure_secret(api: GoogleRest, project: str, name: str, value: str) -> None:
    if not value:
        raise ProvisionError(f"Secret value is empty: {name}")
    base = f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets"
    current = api.request("GET", f"{base}/{name}", allow_404=True)
    if current is None:
        api.request(
            "POST",
            f"{base}?secretId={urllib.parse.quote(name)}",
            {"replication": {"automatic": {}}},
        )
    api.request(
        "POST",
        f"{base}/{name}:addVersion",
        {"payload": {"data": base64.b64encode(value.encode("utf-8")).decode("ascii")}},
    )
    print(f"secret ready: {name}")


def ensure_project_roles(api: GoogleRest, project: str, service_account: str) -> None:
    resource = f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}"
    policy = api.request("POST", f"{resource}:getIamPolicy", {}) or {}
    member = f"serviceAccount:{service_account}"
    bindings = list(policy.get("bindings") or [])
    by_role = {str(item.get("role")): item for item in bindings}
    changed = False
    for role in RUNTIME_ROLES:
        binding = by_role.get(role)
        if binding is None:
            binding = {"role": role, "members": []}
            bindings.append(binding)
            by_role[role] = binding
        members = list(binding.get("members") or [])
        if member not in members:
            members.append(member)
            binding["members"] = members
            changed = True
    if changed:
        policy["bindings"] = bindings
        api.request("POST", f"{resource}:setIamPolicy", {"policy": policy})
    print("runtime IAM roles ready")


def write_local_credentials(path: Path, admin_token: str, cron_token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "dashboard_admin_token": admin_token,
                "dashboard_cron_token": cron_token,
                "note": "Local operator copy. This file is gitignored.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision the AI Content Factory dashboard cloud resources.")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--env-file", default=str(ROOT / ".env.local"))
    parser.add_argument(
        "--credentials-output",
        default=str(ROOT / "factory_dashboard" / ".data" / "dashboard_credentials.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_path = Path(args.env_file).resolve()
    output_path = Path(args.credentials_output).resolve()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip() or load_env_value(env_path, "OPENAI_API_KEY")
    github_token = os.environ.get("GITHUB_DASHBOARD_TOKEN", "").strip() or github_credential()
    previous = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
    admin_token = str(previous.get("dashboard_admin_token") or secrets.token_urlsafe(32))
    cron_token = str(previous.get("dashboard_cron_token") or secrets.token_urlsafe(32))
    if not openai_key:
        raise ProvisionError(f"OPENAI_API_KEY was not found in {env_path}.")
    if not github_token:
        raise ProvisionError("No GitHub credential was available from git credential manager.")

    api = GoogleRest(gcloud_access_token())
    enable_services(api, args.project)
    ensure_repository(api, args.project, args.region)
    ensure_firestore(api, args.project, args.region)
    ensure_project_roles(api, args.project, RUNTIME_SERVICE_ACCOUNT)
    ensure_secret(api, args.project, "factory-dashboard-admin-token", admin_token)
    ensure_secret(api, args.project, "factory-dashboard-cron-token", cron_token)
    ensure_secret(api, args.project, "openai-api-key", openai_key)
    ensure_secret(api, args.project, "factory-dashboard-github-token", github_token)
    write_local_credentials(output_path, admin_token, cron_token)
    print(f"local dashboard credentials: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
