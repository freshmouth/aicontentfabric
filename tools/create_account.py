from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_DIR = ROOT / "accounts"


class CreateAccountError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = create_account(
            account_id=normalize_account_id(args.account),
            display_name=str(args.display_name or args.account).strip(),
            template=str(args.template or "v3_ugc"),
            cadence_days=int(args.cadence_days),
            start_date=str(args.start_date),
            publish_time=str(args.publish_time),
            enable=bool(args.enable),
        )
    except Exception as exc:
        print(f"Create account failed: {exc}")
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an isolated account folder for the generic V3 autopilot.")
    parser.add_argument("--account", required=True, help="New account id, lowercase letters/numbers/underscores.")
    parser.add_argument("--display-name", default="", help="Human display name.")
    parser.add_argument("--template", default="v3_ugc", choices=["v3_ugc", "v3_product", "v3_game"])
    parser.add_argument("--cadence-days", type=int, default=4)
    parser.add_argument("--start-date", default="2026-08-03")
    parser.add_argument("--publish-time", default="12:00")
    parser.add_argument("--enable", action="store_true", help="Enable immediately in accounts/registry.json.")
    return parser


def create_account(
    *,
    account_id: str,
    display_name: str,
    template: str,
    cadence_days: int,
    start_date: str,
    publish_time: str,
    enable: bool,
) -> dict[str, Any]:
    account_dir = ACCOUNTS_DIR / account_id
    if account_dir.exists():
        raise CreateAccountError(f"Account already exists: {account_dir}")
    for subdir in ("content_library", "logs", "prompts", "references", "runs"):
        (account_dir / subdir).mkdir(parents=True, exist_ok=True)

    prefix = account_id.upper()
    write_json(
        account_dir / "account.json",
        {
            "account_id": account_id,
            "display_name": display_name,
            "status": "draft",
            "pipeline": "v3",
            "description": f"Independent V3 account for {display_name}. No cross-account assets or publishing credentials.",
            "paths": {
                "prompts": f"accounts/{account_id}/prompts",
                "content_library": f"accounts/{account_id}/content_library",
                "runs": f"accounts/{account_id}/runs",
                "logs": f"accounts/{account_id}/logs",
            },
        },
    )
    write_json(
        account_dir / "publish_config.json",
        {
            "account_id": account_id,
            "display_name": display_name,
            "publisher": "metricool",
            "require_video_manifest": False,
            "metricool": {
                "base_url": "https://app.metricool.com/api",
                "user_id": f"{prefix}_METRICOOL_USER_ID",
                "blog_id": f"{prefix}_METRICOOL_BLOG_ID",
                "api_token_env": f"{prefix}_METRICOOL_API_TOKEN",
                "timezone": "America/Mexico_City",
                "networks": {"instagram": "instagram", "facebook": "facebook"},
                "post_types": {"instagram": "REEL", "facebook": "REEL"},
                "auto_publish": True,
                "draft": False,
                "show_reel_on_feed": True,
                "save_external_media_files": True,
                "video_cover_milliseconds": 0,
            },
        },
    )
    write_json(
        account_dir / "autopilot_v3.json",
        {
            "account_id": account_id,
            "pipeline": "v3",
            "enabled": False,
            "timezone": "America/Mexico_City",
            "start_date": start_date,
            "interval_days": cadence_days,
            "publish_time": publish_time,
            "platforms": "instagram,facebook",
            "postprocess_preset": "ugc_soft_30fps",
            "output_gcs_uri_prefix": f"gs://ai-content-factory-501821-omni-outputs/accounts/{account_id}/autopilot",
            "first_frame": {
                "enabled": True,
                "with_cloudinary_refs": True,
                "openai_model": "gpt-image-2",
                "openai_size": "720x1280",
                "openai_quality": "medium",
                "timeout_seconds": 240,
            },
            "concepts": [
                {
                    "concept_id": "example_concept",
                    "v3_config": "config.v3.example.json",
                    "caption": "Replace this with a contextual open-loop caption. Keep the same CTA keyword as the video.",
                }
            ],
        },
    )
    write_json(account_dir / "queue.example.json", {"account_id": account_id, "posts": []})
    (account_dir / "secrets.env.example").write_text(
        f"{prefix}_METRICOOL_API_TOKEN=\n{prefix}_METRICOOL_USER_ID=\n{prefix}_METRICOOL_BLOG_ID=\n",
        encoding="utf-8",
    )
    (account_dir / "generation_rules.md").write_text(
        f"# {display_name} Generation Rules\n\n"
        "- Keep public captions contextual and open-looped.\n"
        "- Do not copy the video script exactly into the post description.\n"
        "- Keep CTA keywords consistent between video and caption.\n"
        "- Do not use assets, characters, references, or credentials from other accounts.\n",
        encoding="utf-8",
    )
    update_registry(account_id, enable=enable)
    return {
        "status": "created",
        "account_id": account_id,
        "template": template,
        "enabled_in_registry": enable,
        "account_dir": str(account_dir),
    }


def update_registry(account_id: str, *, enable: bool) -> None:
    registry_path = ACCOUNTS_DIR / "registry.json"
    registry = read_json(registry_path) if registry_path.exists() else {"schema_version": 1, "accounts": []}
    accounts = [dict(item) for item in list(registry.get("accounts") or [])]
    if any(str(item.get("account_id") or "") == account_id for item in accounts):
        raise CreateAccountError(f"Account already exists in registry: {account_id}")
    accounts.append(
        {
            "account_id": account_id,
            "enabled": enable,
            "pipeline": "v3",
            "autopilot_config": f"accounts/{account_id}/autopilot_v3.json",
        }
    )
    registry["accounts"] = accounts
    write_json(registry_path, registry)


def normalize_account_id(account_id: str) -> str:
    value = str(account_id or "").strip().lower().replace("-", "_")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_")
    if not value or any(char not in allowed for char in value) or value.startswith("_") or value.endswith("_"):
        raise CreateAccountError(f"Invalid account id: {account_id}")
    return value


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise CreateAccountError(f"Expected object JSON: {path}")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
