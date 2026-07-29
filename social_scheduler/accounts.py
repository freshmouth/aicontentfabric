from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_ROOT = ROOT / "accounts"

ACCOUNT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]*[a-z0-9]$")


class AccountConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountContext:
    account_id: str
    account_dir: Path
    account_config_path: Path
    publish_config_path: Path
    secrets_env_path: Path
    queue_path: Path
    account_config: dict[str, Any]
    publish_config: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "display_name": self.account_config.get("display_name", self.account_id),
            "status": self.account_config.get("status", "draft"),
            "account_dir": str(self.account_dir),
            "publish_config": str(self.publish_config_path),
            "secrets_env": str(self.secrets_env_path),
            "secrets_env_present": self.secrets_env_path.exists(),
            "queue": str(self.queue_path),
        }


def validate_account_id(account_id: str) -> str:
    normalized = str(account_id or "").strip().lower().replace("-", "_")
    if not normalized:
        raise AccountConfigError("Account id is required.")
    if not ACCOUNT_ID_PATTERN.fullmatch(normalized):
        raise AccountConfigError("Invalid account id. Use lowercase letters, numbers, and underscores only.")
    return normalized


def load_account_context(account_id: str) -> AccountContext:
    normalized = validate_account_id(account_id)
    account_dir = ACCOUNTS_ROOT / normalized
    account_config_path = account_dir / "account.json"
    publish_config_path = account_dir / "publish_config.json"
    secrets_env_path = account_dir / "secrets.env"
    queue_path = account_dir / "queue.json"

    if not account_config_path.exists():
        raise AccountConfigError(f"Missing account config: {account_config_path}")
    if not publish_config_path.exists():
        raise AccountConfigError(f"Missing publish config: {publish_config_path}")

    account_config = read_json(account_config_path)
    publish_config = read_json(publish_config_path)
    declared_account_id = validate_account_id(str(account_config.get("account_id") or normalized))
    publish_account_id = validate_account_id(str(publish_config.get("account_id") or normalized))
    if declared_account_id != normalized:
        raise AccountConfigError(f"Account folder {normalized} declares mismatched account_id {declared_account_id}.")
    if publish_account_id != normalized:
        raise AccountConfigError(f"Publish config for {normalized} declares mismatched account_id {publish_account_id}.")

    return AccountContext(
        account_id=normalized,
        account_dir=account_dir,
        account_config_path=account_config_path,
        publish_config_path=publish_config_path,
        secrets_env_path=secrets_env_path,
        queue_path=queue_path,
        account_config=account_config,
        publish_config=publish_config,
    )


def list_account_summaries() -> list[dict[str, Any]]:
    if not ACCOUNTS_ROOT.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for path in sorted(ACCOUNTS_ROOT.iterdir()):
        if not path.is_dir() or not (path / "account.json").exists():
            continue
        try:
            summaries.append(load_account_context(path.name).summary())
        except AccountConfigError as exc:
            summaries.append({"account_id": path.name, "status": "invalid", "account_dir": str(path), "error": str(exc)})
    return summaries


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AccountConfigError(f"Expected object JSON in {path}")
    return data
