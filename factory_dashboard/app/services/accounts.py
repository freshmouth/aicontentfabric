from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class AccountCatalogError(RuntimeError):
    pass


class AccountCatalog:
    def __init__(self, settings, store) -> None:
        self.settings = settings
        self.store = store

    def list_accounts(self) -> list[dict[str, Any]]:
        registry = self._read_json(self.settings.registry_path)
        accounts: list[dict[str, Any]] = []
        for entry in registry.get("accounts", []):
            account_id = str(entry.get("account_id") or "").strip()
            if not account_id:
                continue
            accounts.append(self.get_account(account_id, registry_entry=dict(entry)))
        return accounts

    def get_account(self, account_id: str, *, registry_entry: dict[str, Any] | None = None) -> dict[str, Any]:
        account_dir = self._account_dir(account_id)
        registry_entry = registry_entry or self._registry_entry(account_id)
        account_path = account_dir / "account.json"
        account = self._read_json(account_path) if account_path.exists() else {}
        autopilot_path = self._autopilot_path(account_dir, registry_entry)
        autopilot = self._read_json(autopilot_path) if autopilot_path.exists() else {}
        override = self.store.get("account_overrides", account_id) or {}
        concepts = list(autopilot.get("concepts") or [])
        effective_enabled = bool(override.get("enabled", registry_entry.get("enabled", False)))
        interval_days = int(override.get("interval_days") or autopilot.get("interval_days") or 1)
        publish_time = str(
            override.get("publish_time")
            or autopilot.get("publish_time")
            or autopilot.get("daily_run_time")
            or "12:00"
        )
        timezone = str(override.get("timezone") or autopilot.get("timezone") or "America/Mexico_City")
        ready = bool(autopilot and concepts)
        return {
            "account_id": account_id,
            "display_name": account.get("display_name") or account_id.replace("_", " ").title(),
            "description": account.get("description") or "",
            "pipeline": registry_entry.get("pipeline") or account.get("pipeline") or "v3",
            "enabled": effective_enabled,
            "registry_enabled": bool(registry_entry.get("enabled", False)),
            "status": "active" if effective_enabled and ready else "setup_required" if not ready else "paused",
            "ready": ready,
            "timezone": timezone,
            "interval_days": interval_days,
            "publish_time": publish_time,
            "platforms": str(autopilot.get("platforms") or "instagram,facebook"),
            "start_date": str(autopilot.get("start_date") or ""),
            "concepts": [
                {
                    "concept_id": item.get("concept_id"),
                    "caption": item.get("caption") or "",
                    "v3_config": item.get("v3_config") or "",
                }
                for item in concepts
            ],
            "override": override,
        }

    def update_schedule(self, account_id: str, values: dict[str, Any]) -> dict[str, Any]:
        self.get_account(account_id)
        current = self.store.get("account_overrides", account_id) or {"account_id": account_id}
        current.update({key: value for key, value in values.items() if value is not None})
        current["account_id"] = account_id
        current["updated_at"] = datetime.utcnow().isoformat() + "Z"
        self.store.put("account_overrides", account_id, current)
        return self.get_account(account_id)

    def generation_template(self, account_id: str, concept_id: str | None = None) -> dict[str, Any]:
        account_dir = self._account_dir(account_id)
        registry_entry = self._registry_entry(account_id)
        autopilot_path = self._autopilot_path(account_dir, registry_entry)
        if not autopilot_path.exists():
            raise AccountCatalogError(f"Account {account_id} does not have an autopilot_v3.json yet.")
        autopilot = self._read_json(autopilot_path)
        concepts = list(autopilot.get("concepts") or [])
        if not concepts:
            raise AccountCatalogError(f"Account {account_id} has no generation concepts.")
        selected = next((item for item in concepts if item.get("concept_id") == concept_id), concepts[0])
        wrapper_path = self._inside(account_dir, account_dir / str(selected["v3_config"]))
        wrapper = self._read_json(wrapper_path)
        source_path = self._inside(account_dir, wrapper_path.parent / str(wrapper["source_config"]))
        source = self._read_json(source_path)
        return {
            "account": self.get_account(account_id),
            "concept": dict(selected),
            "wrapper": wrapper,
            "source_config": source,
        }

    def due_accounts(self, now: datetime | None = None) -> list[dict[str, Any]]:
        due: list[dict[str, Any]] = []
        for account in self.list_accounts():
            if not account["enabled"] or not account["ready"]:
                continue
            timezone = ZoneInfo(account["timezone"])
            local_now = now.astimezone(timezone) if now else datetime.now(timezone)
            start = date.fromisoformat(account["start_date"] or local_now.date().isoformat())
            if local_now.date() < start:
                continue
            if (local_now.date() - start).days % account["interval_days"] == 0:
                due.append({**account, "local_date": local_now.date().isoformat()})
        return due

    def select_due_concept(self, account: dict[str, Any], local_date: str) -> dict[str, Any]:
        concepts = account["concepts"]
        start = date.fromisoformat(account["start_date"] or local_date)
        current = date.fromisoformat(local_date)
        cycle = max(0, (current - start).days // max(1, int(account["interval_days"])))
        return dict(concepts[cycle % len(concepts)])

    def _registry_entry(self, account_id: str) -> dict[str, Any]:
        registry = self._read_json(self.settings.registry_path)
        for item in registry.get("accounts", []):
            if item.get("account_id") == account_id:
                return dict(item)
        raise AccountCatalogError(f"Unknown account: {account_id}")

    def _account_dir(self, account_id: str) -> Path:
        if not account_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in account_id):
            raise AccountCatalogError("Invalid account id.")
        path = self.settings.accounts_dir / account_id
        return path

    def _autopilot_path(self, account_dir: Path, registry_entry: dict[str, Any]) -> Path:
        raw = str(registry_entry.get("autopilot_config") or "autopilot_v3.json")
        path = self.settings.root / raw if raw.startswith("accounts/") else account_dir / raw
        return self._inside(account_dir, path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise AccountCatalogError(f"Expected object JSON: {path}")
        return data

    @staticmethod
    def _inside(root: Path, path: Path) -> Path:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
        return resolved
