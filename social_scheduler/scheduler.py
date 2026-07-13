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

from .gcs_hosting import ensure_public_video_url
from .meta_graph import MetaGraphClient, MetaGraphError, env_secret, load_config, open_json


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUEUE = ROOT / "social_scheduler" / "queue.json"


class SchedulerError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env(find_env_file(args.env))
    try:
        if args.command == "queue":
            record = queue_post(args)
            print(json.dumps(record, indent=2))
        elif args.command == "run-due":
            result = run_due(args)
            print(json.dumps(result, indent=2))
            if any(item.get("status") not in {"published", "dry_run"} for item in result.get("results", [])):
                return 2
        elif args.command == "publish-now":
            result = publish_now(args)
            print(json.dumps(result, indent=2))
            if result.get("status") not in {"published", "dry_run"}:
                return 2
        elif args.command == "status":
            print(json.dumps(read_queue(Path(args.queue)), indent=2))
        elif args.command == "discover-accounts":
            print(json.dumps(discover_accounts(read_json(Path(args.config))), indent=2))
        else:
            parser.print_help()
            return 1
    except Exception as exc:
        print(f"Social scheduler failed: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Schedule and publish finished videos to Instagram/Facebook via Meta Graph.")
    parser.add_argument("--config", default="social_scheduler/config.meta.example.json")
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE))
    parser.add_argument("--env", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    queue = sub.add_parser("queue", help="Add a post to the local schedule queue.")
    queue.add_argument("--video", required=True, help="Local MP4 path.")
    queue.add_argument("--public-video-url", default="", help="Public HTTPS MP4 URL required for Instagram Reels.")
    queue.add_argument("--caption", required=True)
    queue.add_argument("--platforms", default="instagram,facebook")
    queue.add_argument("--publish-at", default="now", help="ISO timestamp or 'now'. Local scheduler time, UTC recommended.")
    queue.add_argument("--dry-run", action="store_true")

    run_due_parser = sub.add_parser("run-due", help="Publish all queued posts whose publish_at is due.")
    run_due_parser.add_argument("--limit", type=int, default=10)
    run_due_parser.add_argument("--dry-run", action="store_true")

    publish_now_parser = sub.add_parser("publish-now", help="Publish one video immediately without queuing.")
    publish_now_parser.add_argument("--video", required=True)
    publish_now_parser.add_argument("--public-video-url", default="")
    publish_now_parser.add_argument("--caption", required=True)
    publish_now_parser.add_argument("--platforms", default="instagram,facebook")
    publish_now_parser.add_argument("--skip-hosting", action="store_true")
    publish_now_parser.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="Print the local queue file.")
    sub.add_parser("discover-accounts", help="Discover visible Facebook Pages and Instagram account IDs from configured tokens.")
    return parser


def queue_post(args: argparse.Namespace) -> dict[str, Any]:
    queue_path = Path(args.queue)
    queue_data = read_queue(queue_path)
    record = {
        "id": uuid.uuid4().hex[:12],
        "status": "queued",
        "created_at": now_iso(),
        "publish_at": normalize_publish_at(args.publish_at),
        "video": str(Path(args.video).resolve()),
        "public_video_url": str(args.public_video_url).strip(),
        "caption": str(args.caption),
        "platforms": parse_platforms(args.platforms),
        "dry_run": bool(args.dry_run),
        "attempts": [],
    }
    queue_data["posts"].append(record)
    write_queue(queue_path, queue_data)
    return record


def run_due(args: argparse.Namespace) -> dict[str, Any]:
    queue_path = Path(args.queue)
    config = read_json(Path(args.config))
    queue_data = read_queue(queue_path)
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
    record = {
        "id": "publish_now",
        "status": "queued",
        "video": str(Path(args.video).resolve()),
        "public_video_url": str(args.public_video_url).strip(),
        "caption": str(args.caption),
        "platforms": parse_platforms(args.platforms),
        "attempts": [],
    }
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
    client = MetaGraphClient(load_config(config))
    attempt = {"at": now_iso(), "dry_run": dry_run, "platforms": {}}
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
                attempt["platforms"]["facebook"] = client.publish_facebook_reel(video_path=video_path, caption=caption)
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


def discover_accounts(config: dict[str, Any]) -> dict[str, Any]:
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


def load_env(path: Path | None) -> None:
    if not path or not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
