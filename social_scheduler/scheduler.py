from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .accounts import AccountConfigError, list_account_summaries, load_account_context, validate_account_id
from .gcs_hosting import ensure_public_video_url
from .meta_graph import MetaGraphClient, MetaGraphError, env_secret, load_config, open_json
from .metricool import MetricoolClient, MetricoolError, load_config as load_metricool_config


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "social_scheduler" / "config.meta.example.json"
DEFAULT_QUEUE = ROOT / "social_scheduler" / "queue.json"
GENERIC_TOKEN_ENV_NAMES = {"INSTAGRAM_ACCESS_TOKEN", "FACEBOOK_PAGE_ACCESS_TOKEN"}
GENERIC_METRICOOL_ENV_NAMES = {"METRICOOL_API_TOKEN", "METRICOOL_USER_ID", "METRICOOL_BLOG_ID"}


class SchedulerError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env(find_env_file(args.env))
    try:
        resolve_cli_context(args)
        load_env(getattr(args, "account_secrets_env", None), override=True)
        if args.command == "queue":
            record = queue_post(args)
            print(json.dumps(record, indent=2))
        elif args.command == "run-due":
            result = run_due(args)
            print(json.dumps(result, indent=2))
            if any(item.get("status") not in {"published", "scheduled", "dry_run"} for item in result.get("results", [])):
                return 2
        elif args.command == "publish-now":
            result = publish_now(args)
            print(json.dumps(result, indent=2))
            if result.get("status") not in {"published", "scheduled", "dry_run"}:
                return 2
        elif args.command == "status":
            queue_data = read_queue(Path(args.queue))
            validate_queue_scope(queue_data, str(args.account or ""))
            print(json.dumps(queue_data, indent=2))
        elif args.command == "discover-accounts":
            print(json.dumps(discover_accounts(read_json(Path(args.config))), indent=2))
        elif args.command == "list-accounts":
            print(json.dumps(list_account_summaries(), indent=2))
        elif args.command == "show-account":
            if not args.account:
                raise SchedulerError("Use --account <account_id> with show-account.")
            print(json.dumps(load_account_context(args.account).summary(), indent=2))
        elif args.command == "validate-account":
            result = validate_account_readiness(args)
            print(json.dumps(result, indent=2))
            if not result.get("ready"):
                return 2
        else:
            parser.print_help()
            return 1
    except Exception as exc:
        print(f"Social scheduler failed: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Schedule and publish finished videos to Instagram/Facebook via account-scoped publishers.")
    parser.add_argument("--account", default="", help="Account id from accounts/<account_id>.")
    parser.add_argument("--config", default="", help="Meta publish config. Defaults to account publish_config.json when --account is set.")
    parser.add_argument("--queue", default="", help="Queue path. Defaults to account queue.json when --account is set.")
    parser.add_argument("--env", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    queue = sub.add_parser("queue", help="Add a post to the local schedule queue.")
    queue.add_argument("--video", required=True, help="Local MP4 path.")
    queue.add_argument("--public-video-url", default="", help="Public HTTPS MP4 URL required for Instagram Reels.")
    queue.add_argument("--caption", required=True)
    queue.add_argument("--first-comment", default="")
    queue.add_argument("--platforms", default="instagram,facebook")
    queue.add_argument("--publish-at", default="now", help="ISO timestamp or 'now'. Local scheduler time, UTC recommended.")
    queue.add_argument("--manifest", default="", help="Optional video/run manifest with account_id for scope validation.")
    queue.add_argument("--dry-run", action="store_true")

    run_due_parser = sub.add_parser("run-due", help="Publish all queued posts whose publish_at is due.")
    run_due_parser.add_argument("--limit", type=int, default=10)
    run_due_parser.add_argument("--dry-run", action="store_true")

    publish_now_parser = sub.add_parser("publish-now", help="Publish one video immediately without queuing.")
    publish_now_parser.add_argument("--video", required=True)
    publish_now_parser.add_argument("--public-video-url", default="")
    publish_now_parser.add_argument("--caption", required=True)
    publish_now_parser.add_argument("--first-comment", default="")
    publish_now_parser.add_argument("--platforms", default="instagram,facebook")
    publish_now_parser.add_argument("--publish-at", default="now", help="ISO timestamp or 'now'. Metricool uses this as the cloud scheduled time.")
    publish_now_parser.add_argument("--manifest", default="", help="Optional video/run manifest with account_id for scope validation.")
    publish_now_parser.add_argument("--skip-hosting", action="store_true")
    publish_now_parser.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="Print the local queue file.")
    sub.add_parser("discover-accounts", help="Discover visible Facebook Pages and Instagram account IDs from configured tokens.")
    sub.add_parser("list-accounts", help="List configured isolated accounts.")
    sub.add_parser("show-account", help="Show the resolved account paths for --account.")
    sub.add_parser("validate-account", help="Validate account config, token env presence, IDs, GCS prefix, and queue scope.")
    return parser


def resolve_cli_context(args: argparse.Namespace) -> None:
    if args.account:
        try:
            ctx = load_account_context(args.account)
        except AccountConfigError as exc:
            raise SchedulerError(str(exc)) from exc
        args.account = ctx.account_id
        if not args.config:
            args.config = str(ctx.publish_config_path)
        if not args.queue:
            args.queue = str(ctx.queue_path)
        args.account_secrets_env = ctx.secrets_env_path
    if not args.config:
        args.config = str(DEFAULT_CONFIG)
    if not args.queue:
        args.queue = str(DEFAULT_QUEUE)
    if not hasattr(args, "account_secrets_env"):
        args.account_secrets_env = None


def queue_post(args: argparse.Namespace) -> dict[str, Any]:
    queue_path = Path(args.queue)
    config = read_json(Path(args.config))
    account_id = validate_config_scope(config, requested_account_id=str(args.account or ""))
    queue_data = read_queue(queue_path)
    validate_queue_scope(queue_data, account_id)
    record = {
        "id": uuid.uuid4().hex[:12],
        "status": "queued",
        "created_at": now_iso(),
        "publish_at": normalize_publish_at(args.publish_at),
        "video": str(Path(args.video).resolve()),
        "public_video_url": str(args.public_video_url).strip(),
        "caption": str(args.caption),
        "first_comment": str(args.first_comment or ""),
        "platforms": parse_platforms(args.platforms),
        "dry_run": bool(args.dry_run),
        "attempts": [],
    }
    if account_id:
        record["account_id"] = account_id
    if str(args.manifest or "").strip():
        record["manifest"] = str(Path(args.manifest).resolve())
    queue_data["posts"].append(record)
    write_queue(queue_path, queue_data)
    return record


def run_due(args: argparse.Namespace) -> dict[str, Any]:
    queue_path = Path(args.queue)
    config = read_json(Path(args.config))
    account_id = validate_config_scope(config, requested_account_id=str(args.account or ""))
    queue_data = read_queue(queue_path)
    validate_queue_scope(queue_data, account_id)
    due = [
        post
        for post in queue_data["posts"]
        if post.get("status") in {"queued", "failed_retryable"} and is_due(str(post.get("publish_at") or ""))
    ]
    results: list[dict[str, Any]] = []
    for post in due[: max(1, int(args.limit))]:
        result = publish_record(post, config=config, dry_run=bool(args.dry_run) or bool(post.get("dry_run")))
        results.append(result)
    write_queue(queue_path, queue_data)
    return {"processed": len(results), "results": results}


def publish_now(args: argparse.Namespace) -> dict[str, Any]:
    config = read_json(Path(args.config))
    account_id = validate_config_scope(config, requested_account_id=str(args.account or ""))
    record = {
        "id": "publish_now",
        "status": "queued",
        "video": str(Path(args.video).resolve()),
        "public_video_url": str(args.public_video_url).strip(),
        "caption": str(args.caption),
        "first_comment": str(args.first_comment or ""),
        "publish_at": normalize_publish_at(args.publish_at),
        "platforms": parse_platforms(args.platforms),
        "attempts": [],
    }
    if account_id:
        record["account_id"] = account_id
    if str(args.manifest or "").strip():
        record["manifest"] = str(Path(args.manifest).resolve())
    return publish_record(record, config=config, dry_run=bool(args.dry_run), skip_hosting=bool(args.skip_hosting))


def publish_record(
    record: dict[str, Any],
    *,
    config: dict[str, Any],
    dry_run: bool,
    skip_hosting: bool = False,
) -> dict[str, Any]:
    video_path = Path(str(record.get("video") or "")).resolve()
    if not video_path.exists() or video_path.stat().st_size <= 0:
        raise SchedulerError(f"Video does not exist: {video_path}")
    caption = str(record.get("caption") or "").strip()
    if not caption:
        raise SchedulerError("Caption is required.")
    platforms = list(record.get("platforms") or [])
    scope = validate_publish_scope(record, config=config, video_path=video_path)
    publisher = publisher_name(config)
    attempt = {"at": now_iso(), "dry_run": dry_run, "platforms": {}}
    attempt["publisher"] = publisher
    if scope.get("account_id"):
        attempt["account_id"] = scope["account_id"]
    if scope.get("manifest_path"):
        attempt["manifest"] = {
            "path": scope["manifest_path"],
            "account_id": scope.get("manifest_account_id", ""),
        }
    if publisher == "metricool":
        return publish_record_metricool(
            record,
            config=config,
            video_path=video_path,
            caption=caption,
            platforms=platforms,
            attempt=attempt,
            dry_run=dry_run,
        )
    client = MetaGraphClient(load_config(config))
    if not skip_hosting and "instagram" in platforms:
        hosted = ensure_public_video_url(video_path, record, config)
        if hosted:
            attempt["hosted_video"] = hosted
    if dry_run:
        for platform in platforms:
            attempt["platforms"][platform] = {"status": "dry_run", "video": str(video_path), "caption": caption}
        record["status"] = "dry_run"
        record.setdefault("attempts", []).append(attempt)
        return {"id": record.get("id"), "status": record["status"], "attempt": attempt}

    failed_platforms: list[str] = []
    successful_platforms: list[str] = []
    for platform in platforms:
        try:
            if platform == "instagram":
                attempt["platforms"]["instagram"] = client.publish_instagram_reel(
                    video_url=str(record.get("public_video_url") or ""),
                    caption=caption,
                    share_to_feed=True,
                )
            elif platform == "facebook":
                attempt["platforms"]["facebook"] = client.publish_facebook_reel(
                    video_path=video_path,
                    caption=caption,
                    video_url=str(record.get("public_video_url") or ""),
                )
            successful_platforms.append(platform)
        except (MetaGraphError, SchedulerError) as exc:
            failed_platforms.append(platform)
            attempt["platforms"][platform] = {"status": "failed", "error": str(exc)}

    attempt["successful_platforms"] = successful_platforms
    attempt["failed_platforms"] = failed_platforms
    if failed_platforms:
        record.setdefault("requested_platforms", list(platforms))
        record["platforms"] = failed_platforms
        record["status"] = "partial_failed" if successful_platforms else "failed_retryable"
    else:
        record["status"] = "published"
        record["platforms"] = []
        record["published_at"] = now_iso()
    record.setdefault("attempts", []).append(attempt)
    return {"id": record.get("id"), "status": record["status"], "attempt": attempt}


def publish_record_metricool(
    record: dict[str, Any],
    *,
    config: dict[str, Any],
    video_path: Path,
    caption: str,
    platforms: list[str],
    attempt: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    metricool_config = load_metricool_config(config)
    client = MetricoolClient(metricool_config)
    publish_at = str(record.get("publish_at") or "now")
    first_comment = str(record.get("first_comment") or "").strip()
    payload = client.build_scheduled_post_payload(
        caption=caption,
        media_url="https://metricool-upload.example/redacted-video.mp4",
        platforms=platforms,
        publish_at=publish_at,
        first_comment=first_comment,
    )
    if dry_run:
        attempt["platforms"]["metricool"] = {
            "status": "dry_run",
            "video": str(video_path),
            "caption": caption,
            "publish_at": publish_at,
            "payload": payload,
        }
        record["status"] = "dry_run"
        record.setdefault("attempts", []).append(attempt)
        return {"id": record.get("id"), "status": record["status"], "attempt": attempt}

    try:
        result = client.schedule_reel(
            video_path=video_path,
            caption=caption,
            platforms=platforms,
            publish_at=publish_at,
            first_comment=first_comment,
            job_id=str(record.get("id") or ""),
        )
        attempt["platforms"]["metricool"] = result
        attempt["successful_platforms"] = platforms
        attempt["failed_platforms"] = []
        record["status"] = "scheduled"
        record["platforms"] = []
        record["scheduled_at"] = now_iso()
        record.setdefault("attempts", []).append(attempt)
        return {"id": record.get("id"), "status": record["status"], "attempt": attempt}
    except (MetricoolError, SchedulerError) as exc:
        attempt["platforms"]["metricool"] = {"status": "failed", "error": str(exc)}
        attempt["successful_platforms"] = []
        attempt["failed_platforms"] = platforms
        record["status"] = "failed_retryable"
        record.setdefault("attempts", []).append(attempt)
        return {"id": record.get("id"), "status": record["status"], "attempt": attempt}


def discover_accounts(config: dict[str, Any]) -> dict[str, Any]:
    validate_config_scope(config, requested_account_id="")
    meta = load_config(config)
    result: dict[str, Any] = {"facebook": {}, "instagram": {}}

    fb_token = os.environ.get(meta.facebook_access_token_env, "").strip()
    if fb_token:
        result["facebook"]["me"] = graph_get("/me", fb_token, {"fields": "id,name"})
        try:
            result["facebook"]["accounts"] = graph_get(
                "/me/accounts",
                fb_token,
                {"fields": "id,name,instagram_business_account{id,username},access_token"},
            )
        except Exception as exc:
            result["facebook"]["accounts_error"] = str(exc)
    else:
        result["facebook"]["error"] = f"Missing {meta.facebook_access_token_env}"

    ig_token = os.environ.get(meta.instagram_access_token_env, "").strip()
    if ig_token:
        try:
            ig_base = (
                f"https://graph.instagram.com/{meta.graph_version}"
                if ig_token.startswith("IGA")
                else "https://graph.facebook.com/v23.0"
            )
            result["instagram"]["me"] = graph_get(
                "/me",
                ig_token,
                {"fields": "id,username,account_type,name"},
                base_url=ig_base,
            )
        except Exception as exc:
            result["instagram"]["me_error"] = str(exc)
    else:
        result["instagram"]["error"] = f"Missing {meta.instagram_access_token_env}"
    return redact_tokens(result)


def validate_account_readiness(args: argparse.Namespace) -> dict[str, Any]:
    if not args.account:
        raise SchedulerError("Use --account <account_id> with validate-account.")
    config = read_json(Path(args.config))
    account_id = validate_config_scope(config, requested_account_id=str(args.account))
    publisher = publisher_name(config)
    queue_data = read_queue(Path(args.queue))
    validate_queue_scope(queue_data, account_id)
    hosting = dict(config.get("google_cloud_storage") or config.get("gcs_hosting") or {})

    checks: list[dict[str, Any]] = []
    checks.append(boolean_check("account_id_matches_config", bool(account_id), account_id))
    checks.append(boolean_check("publisher_selected", publisher in {"meta", "metricool"}, publisher))
    account_secrets_path = getattr(args, "account_secrets_env", None)
    checks.append(
        boolean_check(
            "account_secrets_env_present",
            bool(account_secrets_path and Path(account_secrets_path).exists()),
            str(account_secrets_path or ""),
        )
    )
    if publisher == "metricool":
        metricool = load_metricool_config(config)
        raw_metricool = dict(config.get("metricool") or {})
        user_ready, user_detail = identifier_readiness(raw_metricool, "user_id", metricool.user_id)
        blog_ready, blog_detail = identifier_readiness(raw_metricool, "blog_id", metricool.blog_id)
        checks.append(boolean_check("metricool_user_id_resolved", user_ready, user_detail))
        checks.append(boolean_check("metricool_blog_id_resolved", blog_ready, blog_detail))
        checks.append(
            boolean_check(
                "metricool_token_env_present",
                bool(os.environ.get(metricool.api_token_env, "").strip()),
                metricool.api_token_env,
            )
        )
    else:
        meta = load_config(config)
        checks.append(boolean_check("instagram_user_id_resolved", is_numeric_id(meta.instagram_user_id), redact_identifier(meta.instagram_user_id)))
        checks.append(boolean_check("facebook_page_id_resolved", is_numeric_id(meta.facebook_page_id), redact_identifier(meta.facebook_page_id)))
        checks.append(
            boolean_check(
                "instagram_token_env_present",
                bool(os.environ.get(meta.instagram_access_token_env, "").strip()),
                meta.instagram_access_token_env,
            )
        )
        checks.append(
            boolean_check(
                "facebook_token_env_present",
                bool(os.environ.get(meta.facebook_access_token_env, "").strip()),
                meta.facebook_access_token_env,
            )
        )
        gcs_prefix = str(hosting.get("prefix") or "").strip()
        checks.append(boolean_check("gcs_prefix_is_account_scoped", f"accounts/{account_id}/" in gcs_prefix.replace("\\", "/"), gcs_prefix))
    checks.append(boolean_check("queue_is_account_scoped", queue_data.get("account_id") == account_id, str(args.queue)))

    return {
        "account_id": account_id,
        "ready": all(item["ok"] for item in checks),
        "config": str(Path(args.config).resolve()),
        "queue": str(Path(args.queue).resolve()),
        "checks": checks,
    }


def boolean_check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def is_numeric_id(value: str) -> bool:
    return bool(str(value or "").strip()) and str(value).strip().isdigit()


def redact_identifier(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if not value.isdigit():
        return value
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def identifier_readiness(config: dict[str, Any], field: str, resolved: str) -> tuple[bool, str]:
    explicit_env = str(config.get(f"{field}_env") or "").strip()
    if explicit_env:
        value = os.environ.get(explicit_env, "").strip()
        return bool(value), explicit_env if not value else redact_identifier(value)
    raw_value = str(config.get(field) or "").strip()
    if looks_like_env_name(raw_value):
        value = os.environ.get(raw_value, "").strip()
        return bool(value), raw_value if not value else redact_identifier(value)
    return bool(str(resolved or "").strip()), redact_identifier(resolved)


def looks_like_env_name(value: str) -> bool:
    return bool(value) and value.upper() == value and value.replace("_", "").isalnum()


def graph_get(path: str, token: str, params: dict[str, str], *, base_url: str = "https://graph.facebook.com/v23.0") -> dict[str, Any]:
    query = dict(params)
    query["access_token"] = token
    import urllib.parse
    import urllib.request

    url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "ai-content-factory-meta-scheduler/1.0"})
    return open_json(request)


def redact_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key.lower() in {"access_token", "token"}:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_tokens(item)
        return redacted
    if isinstance(value, list):
        return [redact_tokens(item) for item in value]
    return value


def parse_platforms(value: str) -> list[str]:
    platforms = [item.strip().lower() for item in value.split(",") if item.strip()]
    allowed = {"instagram", "facebook"}
    invalid = sorted(set(platforms) - allowed)
    if invalid:
        raise SchedulerError(f"Unsupported platforms: {', '.join(invalid)}")
    return platforms


def publisher_name(config: dict[str, Any]) -> str:
    value = str(config.get("publisher") or config.get("publish_provider") or "meta").strip().lower()
    aliases = {"meta_graph": "meta", "facebook_graph": "meta"}
    value = aliases.get(value, value)
    if value not in {"meta", "metricool"}:
        raise SchedulerError(f"Unsupported publisher: {value}")
    return value


def config_account_id(config: dict[str, Any]) -> str:
    account_id = str(config.get("account_id") or "").strip()
    if not account_id:
        account = config.get("account")
        if isinstance(account, dict):
            account_id = str(account.get("account_id") or "").strip()
    return validate_account_id(account_id) if account_id else ""


def validate_config_scope(config: dict[str, Any], *, requested_account_id: str = "") -> str:
    account_id = config_account_id(config)
    requested = validate_account_id(requested_account_id) if requested_account_id else ""
    if requested and account_id and requested != account_id:
        raise SchedulerError(f"Requested account {requested} does not match config account {account_id}.")
    effective = account_id or requested
    if effective:
        meta = dict(config.get("meta_graph") or {})
        token_envs = {
            str(meta.get("instagram_access_token_env") or "").strip(),
            str(meta.get("facebook_access_token_env") or "").strip(),
        }
        generic = sorted(item for item in token_envs if item in GENERIC_TOKEN_ENV_NAMES)
        if generic:
            raise SchedulerError(
                "Account-scoped publishing cannot use generic token env vars: "
                + ", ".join(generic)
                + ". Use namespaced env vars such as HYPERDASH_FACEBOOK_PAGE_ACCESS_TOKEN."
            )
        metricool = dict(config.get("metricool") or {})
        metricool_envs = {
            str(metricool.get("api_token_env") or "").strip(),
            str(metricool.get("user_id") or "").strip(),
            str(metricool.get("user_id_env") or "").strip(),
            str(metricool.get("blog_id") or "").strip(),
            str(metricool.get("blog_id_env") or "").strip(),
        }
        metricool_generic = sorted(item for item in metricool_envs if item in GENERIC_METRICOOL_ENV_NAMES)
        if metricool_generic:
            raise SchedulerError(
                "Account-scoped Metricool publishing cannot use generic env vars: "
                + ", ".join(metricool_generic)
                + ". Use namespaced env vars such as HYPERDASH_METRICOOL_API_TOKEN."
            )
    return effective


def validate_queue_scope(queue_data: dict[str, Any], account_id: str) -> None:
    if not account_id:
        return
    queue_account_id = str(queue_data.get("account_id") or "").strip()
    if queue_account_id and validate_account_id(queue_account_id) != account_id:
        raise SchedulerError(f"Queue account {queue_account_id} does not match selected account {account_id}.")
    queue_data["account_id"] = account_id


def validate_publish_scope(record: dict[str, Any], *, config: dict[str, Any], video_path: Path) -> dict[str, str]:
    account_id = validate_config_scope(config, requested_account_id="")
    record_account_id = str(record.get("account_id") or "").strip()
    if record_account_id:
        record_account_id = validate_account_id(record_account_id)
    if account_id and record_account_id and record_account_id != account_id:
        raise SchedulerError(f"Record account {record_account_id} does not match config account {account_id}.")
    if not account_id:
        account_id = record_account_id

    manifest_path, manifest = load_publish_manifest(record, video_path)
    manifest_account_id = extract_manifest_account_id(manifest) if manifest else ""
    if account_id and manifest_account_id and manifest_account_id != account_id:
        raise SchedulerError(f"Manifest account {manifest_account_id} does not match publishing account {account_id}.")
    if account_id and bool(config.get("require_video_manifest", False)) and not manifest:
        raise SchedulerError(
            f"Publishing account {account_id} requires a manifest with account_id. "
            "Pass --manifest or place manifest.json next to the video."
        )
    return {
        "account_id": account_id,
        "manifest_path": str(manifest_path) if manifest_path else "",
        "manifest_account_id": manifest_account_id,
    }


def load_publish_manifest(record: dict[str, Any], video_path: Path) -> tuple[Path | None, dict[str, Any] | None]:
    candidates: list[Path] = []
    if str(record.get("manifest") or "").strip():
        candidates.append(Path(str(record["manifest"])).resolve())
    candidates.extend(
        [
            video_path.parent / "publish_manifest.json",
            video_path.parent / "video_manifest.json",
            video_path.parent / "manifest.json",
        ]
    )
    for path in candidates:
        if path.exists():
            data = read_json(path)
            if isinstance(data, dict):
                return path, data
    return None, None


def extract_manifest_account_id(manifest: dict[str, Any]) -> str:
    for key in ("account_id", "brand_account_id"):
        value = str(manifest.get(key) or "").strip()
        if value:
            return validate_account_id(value)
    account = manifest.get("account")
    if isinstance(account, dict):
        value = str(account.get("account_id") or account.get("id") or "").strip()
        if value:
            return validate_account_id(value)
    return ""


def normalize_publish_at(value: str) -> str:
    if not value or value.strip().lower() == "now":
        return now_iso()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not parsed.tzinfo:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def is_due(value: str) -> bool:
    if not value:
        return True
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() <= time.time()


def read_queue(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"posts": []}
    data = read_json(path)
    if "posts" not in data or not isinstance(data["posts"], list):
        raise SchedulerError(f"Invalid queue file: {path}")
    return data


def write_queue(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_env_file(explicit: str) -> Path | None:
    if explicit:
        return Path(explicit).resolve()
    candidate = ROOT / ".env.local"
    return candidate if candidate.exists() else None


def load_env(path: Path | None, *, override: bool = False) -> None:
    if not path or not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


if __name__ == "__main__":
    raise SystemExit(main())
