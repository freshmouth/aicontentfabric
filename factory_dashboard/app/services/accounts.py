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
        concepts = self._resolvable_concepts(account_dir, autopilot)
        effective_enabled = bool(override.get("enabled", registry_entry.get("enabled", False)))
        interval_days = int(override.get("interval_days") or autopilot.get("interval_days") or 1)
        publish_time = str(
            override.get("publish_time")
            or autopilot.get("publish_time")
            or autopilot.get("daily_run_time")
            or "12:00"
        )
        timezone = str(override.get("timezone") or autopilot.get("timezone") or "America/Mexico_City")
        creative_ready = account_path.exists()
        autopilot_ready = bool(autopilot and concepts)
        publish_ready = bool(registry_entry.get("manual_publish", False)) or (
            account_dir / "publish_config.json"
        ).exists()
        status = (
            "active"
            if effective_enabled and autopilot_ready
            else "paused"
            if autopilot_ready
            else "manual_ready"
            if creative_ready
            else "setup_required"
        )
        return {
            "account_id": account_id,
            "display_name": account.get("display_name") or account_id.replace("_", " ").title(),
            "description": account.get("description") or "",
            "pipeline": registry_entry.get("pipeline") or account.get("pipeline") or "v3",
            "enabled": effective_enabled,
            "registry_enabled": bool(registry_entry.get("enabled", False)),
            "status": status,
            "ready": creative_ready,
            "creative_ready": creative_ready,
            "autopilot_ready": autopilot_ready,
            "publish_ready": publish_ready,
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
        account = self.get_account(account_id)
        if values.get("enabled") is True and not account["autopilot_ready"]:
            raise AccountCatalogError(
                f"Account {account_id} can generate manually, but recurring scheduling requires an autopilot manifest."
            )
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
        concepts = self._resolvable_concepts(account_dir, autopilot)
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

    def creative_template(self, account_id: str) -> dict[str, Any]:
        account = self.get_account(account_id)
        if account["autopilot_ready"]:
            return self.generation_template(account_id)
        if not account["creative_ready"]:
            raise AccountCatalogError(f"Account {account_id} is missing account.json.")
        account_dir = self._account_dir(account_id)
        profile_path = account_dir / "creative_profile.json"
        source = self._read_json(profile_path) if profile_path.exists() else self._manual_source(account)
        source["account_id"] = account_id
        return {
            "account": account,
            "concept": {
                "concept_id": "manual_dashboard_seed",
                "caption": "",
                "v3_config": "",
            },
            "wrapper": {
                "schema_version": 3,
                "account_id": account_id,
                "subject_label": f"account-specific UGC subject for {account['display_name']}",
                "subject_placement_hint": (
                    "Preserve only the people, products, locations, visual identity, and references explicitly "
                    "defined for this account or supplied in the current draft."
                ),
            },
            "source_config": source,
            "manual_mode": True,
        }

    def due_accounts(self, now: datetime | None = None) -> list[dict[str, Any]]:
        due: list[dict[str, Any]] = []
        for account in self.list_accounts():
            if not account["enabled"] or not account["autopilot_ready"]:
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

    def _resolvable_concepts(self, account_dir: Path, autopilot: dict[str, Any]) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        for item in list(autopilot.get("concepts") or []):
            if not isinstance(item, dict) or not str(item.get("v3_config") or "").strip():
                continue
            try:
                wrapper_path = self._inside(account_dir, account_dir / str(item["v3_config"]))
                wrapper = self._read_json(wrapper_path)
                source_path = self._inside(account_dir, wrapper_path.parent / str(wrapper["source_config"]))
                source = self._read_json(source_path)
            except (KeyError, OSError, ValueError, json.JSONDecodeError, AccountCatalogError):
                continue
            if str(source.get("account_id") or "") != str(autopilot.get("account_id") or ""):
                continue
            valid.append(dict(item))
        return valid

    @staticmethod
    def _manual_source(account: dict[str, Any]) -> dict[str, Any]:
        account_id = str(account["account_id"])
        display_name = str(account["display_name"])
        description = str(account.get("description") or "")
        return {
            "name": f"{display_name} manual dashboard seed",
            "concept_id": "manual_dashboard_seed",
            "account_id": account_id,
            "language": "en",
            "defaults": {"aspect_ratio": "9:16", "duration_seconds": 5, "resolution": "720p"},
            "master_prompt": (
                f"Create one coherent native vertical UGC video exclusively for {display_name}. {description} "
                "Use only identity, product, environment, voice, CTA, and visual information explicitly supplied "
                "for this account or in the current creative request. Never borrow assets from another account."
            ),
            "hooks": [
                {
                    "id": "manual_hook",
                    "title": "Manual hook",
                    "duration_seconds": 4,
                    "subject_label": f"{display_name} account-specific UGC subject",
                    "subject_placement_hint": "Use the current brief and attached references only.",
                    "script": "Replace this line with the requested opening hook.",
                    "prompt": "Build the requested native UGC pattern interrupt. Native dialogue: Replace this line.",
                }
            ],
            "mains": [
                {
                    "id": "manual_main",
                    "title": "Manual main",
                    "prompt": "Develop the requested concept without changing identity or account context.",
                    "segments": [
                        {
                            "id": "manual_main_01",
                            "duration_seconds": 5,
                            "subject_label": f"{display_name} account-specific UGC subject",
                            "subject_placement_hint": "Use the current brief and attached references only.",
                            "script": "Replace this line with the core message.",
                            "prompt": "Show one coherent visual beat. Native dialogue: Replace this line.",
                        }
                    ],
                }
            ],
            "ctas": [
                {
                    "id": "manual_cta",
                    "title": "Manual CTA",
                    "duration_seconds": 5,
                    "subject_label": f"{display_name} account-specific UGC subject",
                    "subject_placement_hint": "Use only the CTA requested for this account.",
                    "script": "Replace this line with the requested CTA.",
                    "prompt": "End naturally with the requested CTA. Native dialogue: Replace this line.",
                }
            ],
            "variants": {
                "count": 1,
                "min_total_seconds": 15,
                "max_total_seconds": 45,
                "seed": 20260804,
                "stitch_leaf_segments": True,
            },
        }

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
