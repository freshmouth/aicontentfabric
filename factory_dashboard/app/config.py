from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    root: Path
    accounts_dir: Path
    registry_path: Path
    store_backend: str
    local_data_path: Path
    firestore_collection_prefix: str
    google_cloud_project: str
    upload_bucket: str
    upload_prefix: str
    admin_token: str
    openai_api_key: str
    openai_model: str
    github_token: str
    github_owner: str
    github_repo: str
    github_workflow: str
    github_branch: str
    session_cookie_name: str
    session_days: int

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(os.environ.get("FACTORY_ROOT") or ROOT).resolve()
        return cls(
            root=root,
            accounts_dir=root / "accounts",
            registry_path=root / "accounts" / "registry.json",
            store_backend=(os.environ.get("DASHBOARD_STORE") or "local").strip().lower(),
            local_data_path=Path(
                os.environ.get("DASHBOARD_LOCAL_DATA") or root / "factory_dashboard" / ".data" / "dashboard.json"
            ).resolve(),
            firestore_collection_prefix=(os.environ.get("DASHBOARD_FIRESTORE_PREFIX") or "factory").strip(),
            google_cloud_project=(os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip(),
            upload_bucket=(os.environ.get("DASHBOARD_UPLOAD_BUCKET") or "").strip(),
            upload_prefix=(os.environ.get("DASHBOARD_UPLOAD_PREFIX") or "factory-dashboard/uploads").strip("/"),
            admin_token=(os.environ.get("DASHBOARD_ADMIN_TOKEN") or "").strip(),
            openai_api_key=(os.environ.get("OPENAI_API_KEY") or "").strip(),
            openai_model=(os.environ.get("OPENAI_CREATIVE_MODEL") or "gpt-4.1-mini").strip(),
            github_token=(os.environ.get("GITHUB_DASHBOARD_TOKEN") or "").strip(),
            github_owner=(os.environ.get("GITHUB_OWNER") or "freshmouth").strip(),
            github_repo=(os.environ.get("GITHUB_REPO") or "aicontentfabric").strip(),
            github_workflow=(os.environ.get("GITHUB_WORKFLOW") or "account-autopilot.yml").strip(),
            github_branch=(os.environ.get("GITHUB_BRANCH") or "main").strip(),
            session_cookie_name=(os.environ.get("DASHBOARD_SESSION_COOKIE") or "factory_session").strip(),
            session_days=max(1, int(os.environ.get("DASHBOARD_SESSION_DAYS") or "90")),
        )
