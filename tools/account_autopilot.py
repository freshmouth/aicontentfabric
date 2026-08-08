from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from factory_dashboard.app.services.creative_contract import CreativeContractError, compile_source_config


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_DIR = ROOT / "accounts"
DEFAULT_REGISTRY = ACCOUNTS_DIR / "registry.json"
OMNI_MASTER_PROMPT = (
    "Create ONE vertical 9:16 short-form UGC clip with native spoken audio. "
    "Use the attached reference image as the exact first frame and visual source of truth. "
    "Animate only what naturally belongs in the frame. Preserve the same person, room, objects, "
    "product/object texture, lighting, framing, and phone-camera realism from the reference image. "
    "One leaf scene only: no montage, no location jump, no extra CTA, no repeated line fragments. "
    "Spoken audio must say only the Native dialogue once, naturally and clearly, with no long silence. "
    "Keep claims framed as educational/marketing narrative, not medical advice. No miracle cures, "
    "no disease claims, no fake doctor language, no social UI, no captions baked into generated video."
)


class AccountAutopilotError(RuntimeError):
    pass


class CommandExecutionError(AccountAutopilotError):
    def __init__(self, returncode: int, diagnostic_code: str, detail: str = "") -> None:
        self.returncode = returncode
        self.diagnostic_code = diagnostic_code
        self.detail = detail
        message = f"command_exit={returncode}; diagnostic={diagnostic_code}"
        if detail:
            message += f"; detail={detail}"
        super().__init__(message)


class StageExecutionError(AccountAutopilotError):
    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause_type = str(getattr(cause, "diagnostic_code", type(cause).__name__))
        detail = str(getattr(cause, "detail", "")).strip()
        message = f"stage={self.stage}; cause={self.cause_type}"
        if detail:
            message += f"; detail={detail}"
        super().__init__(message)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env(ROOT / ".env.local")
    try:
        if args.all_due:
            result = run_all_due(args)
        else:
            if not args.account:
                raise AccountAutopilotError("Use --account <account_id> or --all-due.")
            result = run_one_account(normalize_account_id(args.account), args)
    except Exception as exc:
        print(f"Account autopilot failed: {exc}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true" and isinstance(exc, StageExecutionError):
            print(f"::error title=Account autopilot failed::{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Registry-driven multi-account V3 autopilot.")
    parser.add_argument("--account", default="", help="Account id from accounts/<account_id>.")
    parser.add_argument("--all-due", action="store_true", help="Run every enabled account due today.")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="Master account registry JSON.")
    parser.add_argument("--force", action="store_true", help="Run even when today is not on the account cadence.")
    parser.add_argument("--today", default="", help="Override local date for cadence checks, YYYY-MM-DD.")
    parser.add_argument(
        "--publish-at",
        default="",
        help="Override the scheduled publish datetime for this run as ISO-8601, including timezone when possible.",
    )
    parser.add_argument("--concept-id", default="", help="Force a concept_id for a single-account run.")
    parser.add_argument("--plan-only", action="store_true", help="Validate cadence/config without provider calls.")
    parser.add_argument("--dry-run", action="store_true", help="Generate assets but dry-run Metricool publishing.")
    parser.add_argument("--skip-publish", action="store_true", help="Generate video only.")
    parser.add_argument("--request-file", default="", help="Account-scoped dashboard generation request JSON.")
    parser.add_argument("--request-id", default="", help="External request id used for traceable dashboard runs.")
    parser.add_argument("--continue-on-error", action="store_true", help="Keep processing other accounts when one fails.")
    return parser


def run_all_due(args: argparse.Namespace) -> dict[str, Any]:
    registry = read_json(Path(args.registry).resolve())
    entries = [dict(item) for item in list(registry.get("accounts") or [])]
    results: list[dict[str, Any]] = []
    failed = False
    for entry in entries:
        account_id = normalize_account_id(str(entry.get("account_id") or ""))
        if not bool(entry.get("enabled", False)):
            results.append({"account_id": account_id, "status": "disabled"})
            continue
        if str(entry.get("pipeline") or "v3") != "v3":
            results.append({"account_id": account_id, "status": "skipped", "reason": "unsupported_pipeline"})
            continue
        try:
            result = run_one_account(account_id, args, registry_entry=entry)
        except Exception as exc:
            failed = True
            result = {"account_id": account_id, "status": "failed", "error": str(exc)}
            if not args.continue_on_error:
                results.append(result)
                raise
        results.append(result)
    return {
        "status": "failed" if failed else "ok",
        "mode": "all_due",
        "processed": len(results),
        "results": results,
    }


def run_one_account(
    account_id: str,
    args: argparse.Namespace,
    *,
    registry_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    account_dir = ACCOUNTS_DIR / account_id
    load_env(account_dir / "secrets.env", override=True)
    load_account_secret_bundle(account_id)
    account_config_path = account_dir / "account.json"
    if not account_config_path.exists():
        raise AccountAutopilotError(f"Missing account.json for {account_id}: {account_config_path}")
    account_config = read_json(account_config_path)
    assert_account_id(account_config, account_id, "account.json")
    generation_request = load_generation_request(str(args.request_file or ""), expected_account_id=account_id)
    autopilot_path = resolve_autopilot_path(account_id, account_dir, registry_entry)
    if autopilot_path.exists():
        autopilot_config = read_json(autopilot_path)
        assert_account_id(autopilot_config, account_id, "autopilot_v3.json")
    elif generation_request:
        autopilot_config = build_manual_execution_config(account_id, account_dir)
    else:
        raise AccountAutopilotError(f"Missing autopilot config for {account_id}: {autopilot_path}")
    return run_autopilot(
        account_id,
        account_dir,
        autopilot_path,
        autopilot_config,
        args,
        generation_request=generation_request,
        account_config=account_config,
    )


def build_manual_execution_config(account_id: str, account_dir: Path) -> dict[str, Any]:
    publish_path = account_dir / "publish_config.json"
    publish_config = read_json(publish_path) if publish_path.exists() else {}
    metricool = dict(publish_config.get("metricool") or {})
    networks = dict(metricool.get("networks") or {})
    platforms = ",".join(key for key in ("instagram", "facebook") if key in networks) or "instagram,facebook"
    return {
        "account_id": account_id,
        "pipeline": "v3_manual_dashboard",
        "enabled": True,
        "timezone": str(metricool.get("timezone") or "America/Mexico_City"),
        "start_date": datetime.now().date().isoformat(),
        "interval_days": 1,
        "publish_time": "12:00",
        "platforms": platforms,
        "postprocess_preset": "ugc_soft_30fps",
        "output_gcs_uri_prefix": (
            f"gs://ai-content-factory-501821-omni-outputs/accounts/{account_id}/manual-dashboard"
        ),
        "first_frame": {
            "enabled": True,
            "with_cloudinary_refs": False,
            "openai_model": "gpt-image-2",
            "openai_size": "720x1280",
            "openai_quality": "medium",
            "timeout_seconds": 300,
        },
        "concepts": [],
    }


def build_manual_wrapper(
    account_id: str,
    account_config: dict[str, Any],
    source_config: dict[str, Any],
) -> dict[str, Any]:
    display_name = str(account_config.get("display_name") or account_id.replace("_", " ").title())
    master_prompt = str(source_config.get("master_prompt") or "").strip()
    return {
        "schema_version": 3,
        "name": f"{display_name} dashboard manual generation",
        "account_id": account_id,
        "source_config": "dashboard_source_config.json",
        "subject_label": f"account-specific UGC subject for {display_name}",
        "subject_placement_hint": (
            f"Preserve the identity, product, location, voice, and visual continuity defined by this account. "
            f"{master_prompt}"
        )[:6000],
        "cloudinary_max_reference_images": 0,
    }


def download_dashboard_references(
    account_id: str,
    generation_request: dict[str, Any],
    run_dir: Path,
) -> list[Path]:
    attachments = list((generation_request or {}).get("reference_attachments") or [])
    if not attachments:
        return []
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise AccountAutopilotError("Dashboard references require google-cloud-storage.") from exc
    client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
    output_dir = run_dir / "dashboard_references"
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    expected_prefix = f"factory-dashboard/uploads/{account_id}/"
    for index, item in enumerate(attachments, start=1):
        if normalize_account_id(str(item.get("account_id") or "")) != account_id:
            raise AccountAutopilotError("Dashboard reference belongs to a different account.")
        uri = str(item.get("storage_uri") or "")
        if not uri.startswith("gs://"):
            raise AccountAutopilotError("Dashboard generation references must use Cloud Storage URIs.")
        bucket_name, separator, object_name = uri.removeprefix("gs://").partition("/")
        if not separator or not object_name.startswith(expected_prefix):
            raise AccountAutopilotError("Dashboard reference is outside the selected account upload prefix.")
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }.get(str(item.get("content_type") or "").lower())
        if not extension:
            raise AccountAutopilotError("Unsupported dashboard reference image type.")
        output_path = output_dir / f"reference_{index:02d}{extension}"
        client.bucket(bucket_name).blob(object_name).download_to_filename(output_path)
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise AccountAutopilotError(f"Dashboard reference download was empty: {item.get('id')}")
        downloaded.append(output_path)
    return downloaded


def run_autopilot(
    account_id: str,
    account_dir: Path,
    autopilot_path: Path,
    config: dict[str, Any],
    args: argparse.Namespace,
    *,
    generation_request: dict[str, Any] | None = None,
    account_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generation_request = generation_request or load_generation_request(
        str(args.request_file or ""), expected_account_id=account_id
    )
    if not bool(config.get("enabled", True)) and not generation_request:
        return {"status": "disabled", "account_id": account_id}

    tz_name = str(config.get("timezone") or "America/Mexico_City")
    tz = load_timezone(tz_name)
    today = date.fromisoformat(args.today) if args.today else datetime.now(tz).date()
    start_date = date.fromisoformat(str(config.get("start_date") or today.isoformat()))
    interval_days = int(config.get("interval_days") or 1)
    due = bool(generation_request) or is_due(today, start_date=start_date, interval_days=interval_days)
    cycle_index = max(0, (today - start_date).days // max(1, interval_days))
    if generation_request and not list(config.get("concepts") or []):
        concept = {
            "concept_id": generation_request["concept_id"],
            "caption": generation_request.get("caption") or "",
            "v3_config": "",
        }
    else:
        concept = select_concept(config, cycle_index=cycle_index, forced_id=str(args.concept_id or ""))
    if generation_request:
        concept = {
            **concept,
            "concept_id": generation_request["concept_id"],
            "caption": generation_request.get("caption") or concept.get("caption") or "",
        }
    publish_at = resolve_publish_datetime(
        str(args.publish_at or ""),
        today=today,
        tz=tz,
        default_time=str(config.get("publish_time") or "12:00"),
    )
    plan = {
        "status": "due" if due or args.force else "skipped",
        "account_id": account_id,
        "today": today.isoformat(),
        "timezone": tz_name,
        "start_date": start_date.isoformat(),
        "interval_days": interval_days,
        "cycle_index": cycle_index,
        "concept_id": concept["concept_id"],
        "publish_at": publish_at.isoformat(),
        "platforms": str(config.get("platforms") or "instagram,facebook"),
        "autopilot_config": str(autopilot_path),
        "execution_mode": "manual_dashboard" if generation_request else "autopilot",
        "request_id": str(args.request_id or generation_request.get("request_id") or "") if generation_request else "",
    }
    if not due and not args.force:
        return plan

    external_request_id = str(args.request_id or (generation_request or {}).get("request_id") or "").strip()
    run_id = (
        f"dashboard_{slug(external_request_id)}"
        if external_request_id
        else f"autopilot_{today.strftime('%Y%m%d')}_{slug(concept['concept_id'])}"
    )
    run_dir = account_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "autopilot_plan.json", plan)

    wrapper_value = str(concept.get("v3_config") or "").strip()
    if wrapper_value:
        wrapper_path = require_inside(account_dir, resolve_path(account_dir, wrapper_value), "V3 wrapper")
        wrapper_config = read_json(wrapper_path)
    elif generation_request:
        wrapper_path = run_dir / "dashboard_manual_wrapper.json"
        wrapper_config = build_manual_wrapper(account_id, account_config or {}, generation_request["source_config"])
        write_json(wrapper_path, wrapper_config)
    else:
        raise AccountAutopilotError(f"Concept {concept['concept_id']} has no V3 wrapper.")
    if generation_request:
        source_config = dict(generation_request["source_config"])
        source_config_path = run_dir / "dashboard_source_config.json"
        write_json(source_config_path, source_config)
    else:
        source_config_path = require_inside(
            account_dir,
            resolve_path(wrapper_path.parent, str(wrapper_config["source_config"])),
            "V3 source config",
        )
        source_config = read_json(source_config_path)
    assert_account_id(source_config, account_id, source_config_path.name)

    try:
        source_config, creative_preflight = compile_source_config(source_config)
    except CreativeContractError as exc:
        write_json(
            run_dir / "creative_preflight.json",
            {"status": "failed", "account_id": account_id, "error": str(exc)},
        )
        raise StageExecutionError("creative_preflight", exc) from exc
    source_config_path = run_dir / "compiled_source_config.json"
    cloud_route = configure_cloud_route(
        account_id=account_id,
        generation_request=generation_request,
        source_config=source_config,
        config=config,
        run_id=run_id,
    )
    if cloud_route:
        plan["cloud_route"] = public_cloud_route(cloud_route)
    write_json(source_config_path, source_config)
    write_json(
        run_dir / "creative_preflight.json",
        {**creative_preflight, "account_id": account_id, "concept_id": str(concept["concept_id"])},
    )
    if args.plan_only:
        plan["plan_only"] = True
        plan["creative_preflight"] = creative_preflight
        plan["run_dir"] = str(run_dir)
        write_json(run_dir / "autopilot_plan.json", plan)
        append_execution_event(run_dir, "creative_preflight", "succeeded")
        append_execution_event(run_dir, "pipeline", "succeeded")
        return plan

    v3_manifest = run_checked(
        "prepare_v3_manifest",
        lambda: prepare_v3_manifest(account_id, wrapper_config, source_config, source_config_path, run_dir),
        run_dir=run_dir,
    )
    request_references = run_checked(
        "download_dashboard_references",
        lambda: download_dashboard_references(account_id, generation_request, run_dir),
        run_dir=run_dir,
    )
    first_frames = run_checked(
        "generate_first_frames",
        lambda: generate_first_frames(
            account_dir,
            wrapper_config,
            v3_manifest,
            run_dir,
            config,
            explicit_references=request_references,
        ),
        run_dir=run_dir,
    )
    omni_config_path = run_checked(
        "build_omni_config",
        lambda: build_run_omni_config(
            source_config,
            first_frames,
            run_dir,
            config,
            today=today,
            concept_id=str(concept["concept_id"]),
        ),
        run_dir=run_dir,
    )
    omni_dir = run_dir / "omni"
    run_checked("generate_omni_video", lambda: run_omni(omni_config_path, omni_dir, run_dir), run_dir=run_dir)
    final_video = run_checked("resolve_final_video", lambda: resolve_variant_video(omni_dir), run_dir=run_dir)
    captioned_video = run_checked(
        "finish_captions_and_hook",
        lambda: finish_captions_and_hook(final_video, omni_config_path, source_config, run_dir),
        run_dir=run_dir,
    )
    publish_ready = run_checked(
        "postprocess_publish_ready",
        lambda: postprocess_publish_ready(captioned_video, run_dir, config),
        run_dir=run_dir,
    )
    hosted_video = run_checked(
        "persist_final_video",
        lambda: persist_final_video(
            account_id=account_id,
            video_path=publish_ready,
            config=config,
            today=today,
            concept_id=str(concept["concept_id"]),
            run_id=run_id,
            destination_uri=str(cloud_route.get("master_output_gcs_uri") or "") if cloud_route else "",
            result_uri=str(cloud_route.get("result_gcs_uri") or "") if cloud_route else "",
            staging_prefix=str(cloud_route.get("job_staging_gcs_uri_prefix") or "") if cloud_route else "",
            cleanup_staging=bool(cloud_route.get("cleanup_staging", False)) if cloud_route else False,
        ),
        run_dir=run_dir,
    )
    if generation_request and not str(args.publish_at or "").strip():
        publish_at = datetime.now(tz) + timedelta(minutes=10)
        plan["publish_at"] = publish_at.isoformat()
        write_json(run_dir / "autopilot_plan.json", plan)
    video_manifest = run_checked(
        "write_video_manifest",
        lambda: write_video_manifest(account_id, run_dir, publish_ready, concept, publish_at),
        run_dir=run_dir,
    )

    publish_result: dict[str, Any] = {"status": "skipped"}
    if not args.skip_publish:
        publish_result = run_checked(
            "publish_metricool",
            lambda: publish_metricool(
                account_id=account_id,
                video_path=publish_ready,
                manifest_path=video_manifest,
                caption=str(concept["caption"]),
                publish_at=publish_at,
                platforms=str(config.get("platforms") or "instagram,facebook"),
                dry_run=bool(args.dry_run),
                run_dir=run_dir,
            ),
            run_dir=run_dir,
        )
    append_execution_event(run_dir, "pipeline", "succeeded")

    result = {
        **plan,
        "status": "succeeded",
        "run_dir": str(run_dir),
        "v3_manifest": str(run_dir / "v3_execution_manifest.json"),
        "omni_config": str(omni_config_path),
        "final_video": str(final_video),
        "publish_ready": str(publish_ready),
        "hosted_video": hosted_video,
        "video_manifest": str(video_manifest),
        "metricool": publish_result,
    }
    write_json(run_dir / "autopilot_result.json", result)
    return result


def configure_cloud_route(
    *,
    account_id: str,
    generation_request: dict[str, Any] | None,
    source_config: dict[str, Any],
    config: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    route = dict((generation_request or {}).get("cloud_route") or {})
    os.environ.pop("GOOGLE_IMPERSONATE_SERVICE_ACCOUNT", None)
    if not route:
        return {}
    if normalize_account_id(str(route.get("account_id") or account_id)) != account_id:
        raise AccountAutopilotError("Google Cloud route belongs to a different account.")
    master_output = str(route.get("master_output_gcs_uri") or "").strip()
    result_uri = str(route.get("result_gcs_uri") or "").strip()
    if not master_output.startswith("gs://") or not result_uri.startswith("gs://"):
        raise AccountAutopilotError("Dashboard cloud route requires master output and result GCS URIs.")
    account_marker = f"/accounts/{account_id}/"
    if account_marker not in f"{master_output}/" or account_marker not in f"{result_uri}/":
        raise AccountAutopilotError("Master cloud output is not scoped to the selected account.")

    project = str(route.get("generation_project_id") or "").strip()
    service_account = str(route.get("generation_service_account") or "").strip()
    staging = str(route.get("staging_gcs_uri_prefix") or "").strip().rstrip("/")
    configured_values = [project, service_account, staging]
    if any(configured_values) and not all(configured_values):
        raise AccountAutopilotError(
            "Google Cloud generation routing requires project, service account, and staging prefix together."
        )
    if all(configured_values):
        if not service_account.endswith(".iam.gserviceaccount.com"):
            raise AccountAutopilotError("Invalid generation service account.")
        if f"/accounts/{account_id}" not in staging:
            raise AccountAutopilotError("Generation staging is not scoped to the selected account.")
        job_staging = f"{staging}/jobs/{slug(run_id)}"
        provider = dict(source_config.get("google_omni_flash") or source_config.get("provider") or {})
        source_config["google_omni_flash"] = provider
        provider["project_id"] = project
        provider["location"] = str(route.get("generation_location") or "global")
        config["generation_output_gcs_uri_prefix"] = job_staging
        os.environ["GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"] = service_account
        route["job_staging_gcs_uri_prefix"] = job_staging
    return route


def public_cloud_route(route: dict[str, Any]) -> dict[str, Any]:
    return {
        key: route.get(key)
        for key in (
            "account_id",
            "generation_project_id",
            "generation_service_account",
            "generation_location",
            "job_staging_gcs_uri_prefix",
            "master_output_gcs_uri",
            "cleanup_staging",
        )
        if route.get(key) not in (None, "")
    }


def persist_final_video(
    *,
    account_id: str,
    video_path: Path,
    config: dict[str, Any],
    today: date,
    concept_id: str,
    run_id: str,
    destination_uri: str = "",
    result_uri: str = "",
    staging_prefix: str = "",
    cleanup_staging: bool = False,
) -> dict[str, Any]:
    prefix_uri = str(config.get("output_gcs_uri_prefix") or "").strip().rstrip("/")
    if not prefix_uri.startswith("gs://"):
        raise AccountAutopilotError("output_gcs_uri_prefix must be a gs:// URI for durable cloud runs.")
    bucket_name, separator, prefix = prefix_uri.removeprefix("gs://").partition("/")
    if not separator or not bucket_name or not prefix:
        raise AccountAutopilotError(f"Invalid output_gcs_uri_prefix: {prefix_uri}")
    if not video_path.exists() or video_path.stat().st_size <= 0:
        raise AccountAutopilotError(f"Cannot persist missing final video: {video_path}")
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise AccountAutopilotError("Durable cloud output requires google-cloud-storage.") from exc
    if destination_uri:
        bucket_name, object_name = split_gcs_uri(destination_uri)
    else:
        object_name = (
            f"{prefix}/{today.strftime('%Y%m%d')}/{slug(concept_id)}/{slug(run_id)}/final/"
            f"final_video_{slug(account_id)}_{today.strftime('%Y%m%d')}.mp4"
        )
    client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
    blob = client.bucket(bucket_name).blob(object_name)
    local_md5 = base64.b64encode(hashlib.md5(video_path.read_bytes()).digest()).decode("ascii")
    if blob.exists():
        blob.reload()
        if str(blob.md5_hash or "") != local_md5 or int(blob.size or 0) != int(video_path.stat().st_size):
            raise AccountAutopilotError("The master archive object already exists with different content.")
    else:
        blob.upload_from_filename(str(video_path), content_type="video/mp4", if_generation_match=0)
        blob.reload()
    if int(blob.size or 0) != int(video_path.stat().st_size) or str(blob.md5_hash or "") != local_md5:
        raise AccountAutopilotError("Master archive verification failed; temporary outputs were retained.")
    archived = {
        "provider": "google_cloud_storage",
        "bucket": bucket_name,
        "object_name": object_name,
        "gcs_uri": f"gs://{bucket_name}/{object_name}",
        "size_bytes": int(video_path.stat().st_size),
        "md5_hash": local_md5,
        "verified": True,
        "run_id": run_id,
    }
    if result_uri:
        result_bucket, result_object = split_gcs_uri(result_uri)
        if result_bucket != bucket_name:
            raise AccountAutopilotError("Result manifest must use the same master bucket as the final video.")
        client.bucket(result_bucket).blob(result_object).upload_from_string(
            json.dumps({"account_id": account_id, "status": "succeeded", "hosted_video": archived}, indent=2),
            content_type="application/json",
        )
        archived["result_gcs_uri"] = result_uri
    if cleanup_staging and staging_prefix:
        archived["staging_cleanup"] = cleanup_staging_objects(
            client=client,
            staging_prefix=staging_prefix,
            destination_uri=archived["gcs_uri"],
            run_id=run_id,
        )
    return archived


def split_gcs_uri(uri: str) -> tuple[str, str]:
    bucket, separator, object_name = str(uri or "").removeprefix("gs://").partition("/")
    if not separator or not bucket or not object_name:
        raise AccountAutopilotError(f"Invalid Cloud Storage URI: {uri}")
    return bucket, object_name


def cleanup_staging_objects(*, client: Any, staging_prefix: str, destination_uri: str, run_id: str) -> dict[str, Any]:
    bucket_name, prefix = split_gcs_uri(staging_prefix.rstrip("/"))
    destination_bucket, destination_object = split_gcs_uri(destination_uri)
    if slug(run_id) not in slug(prefix):
        raise AccountAutopilotError("Refusing cleanup because the staging prefix is not job-scoped.")
    if bucket_name == destination_bucket and destination_object.startswith(f"{prefix}/"):
        raise AccountAutopilotError("Refusing cleanup because the master video is inside staging.")
    deleted = 0
    for candidate in client.list_blobs(bucket_name, prefix=f"{prefix}/"):
        candidate.delete(if_generation_match=int(candidate.generation))
        deleted += 1
    return {"status": "completed", "deleted_objects": deleted, "prefix": staging_prefix}


def run_checked(stage: str, operation: Any, *, run_dir: Path | None = None) -> Any:
    if run_dir is not None:
        append_execution_event(run_dir, stage, "started")
    try:
        result = operation()
        if run_dir is not None:
            append_execution_event(run_dir, stage, "succeeded")
        return result
    except StageExecutionError:
        if run_dir is not None:
            append_execution_event(run_dir, stage, "failed", "nested stage failure")
        raise
    except Exception as exc:
        if run_dir is not None:
            append_execution_event(run_dir, stage, "failed", str(exc))
        raise StageExecutionError(stage, exc) from exc


def append_execution_event(run_dir: Path, stage: str, status: str, detail: str = "") -> None:
    path = run_dir / "execution_events.json"
    events: list[dict[str, Any]] = []
    if path.exists():
        try:
            current = read_json(path)
            events = list(current.get("events") or []) if isinstance(current, dict) else []
        except (OSError, json.JSONDecodeError):
            events = []
    event = {
        "sequence": len(events) + 1,
        "stage": stage,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if detail:
        event["detail"] = detail[:2000]
    events.append(event)
    overall_status = "failed" if status == "failed" else ("succeeded" if stage == "pipeline" else "in_progress")
    write_json(path, {"status": overall_status, "events": events})


def prepare_v3_manifest(
    account_id: str,
    wrapper_config: dict[str, Any],
    source_config: dict[str, Any],
    source_config_path: Path,
    run_dir: Path,
) -> list[dict[str, Any]]:
    from Mix.v3.image_prompting import build_image_prompt
    from Mix.v3.image_qa import build_qa_prompt
    from Mix.v3.pipeline_v3 import collect_scenes
    from Mix.v3.subject import SubjectDescriptor

    project = {
        "subject_label": wrapper_config.get("subject_label"),
        "subject_placement_hint": wrapper_config.get("subject_placement_hint"),
    }
    records: list[dict[str, Any]] = []
    for index, scene in enumerate(collect_scenes(source_config), start=1):
        subject = SubjectDescriptor.from_mapping(project, scene)
        image_prompt = build_image_prompt(scene, project=project)
        record = {
            "index": index,
            "role": scene.get("role", ""),
            "component_id": scene.get("component_id", ""),
            "scene_id": scene.get("id", f"scene_{index:02d}"),
            "duration_seconds": scene.get("duration_seconds"),
            "subject_label": subject.label,
            "subject_placement_hint": subject.placement_hint,
            "image_prompt": image_prompt,
            "qa_prompt": build_qa_prompt(image_prompt, project=project, scene=scene),
        }
        records.append(record)
        write_json(run_dir / "image_prompts" / f"{index:02d}_{record['scene_id']}.json", record)
    manifest = {
        "schema_version": 3,
        "module": "account_v3_autopilot",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "account_id": account_id,
        "source_config": str(source_config_path),
        "scene_count": len(records),
        "scenes": records,
        "generated_first_images": [],
        "execute_video": True,
    }
    write_json(run_dir / "v3_execution_manifest.json", manifest)
    return records


def generate_first_frames(
    account_dir: Path,
    wrapper_config: dict[str, Any],
    records: list[dict[str, Any]],
    run_dir: Path,
    autopilot_config: dict[str, Any],
    *,
    explicit_references: list[Path] | None = None,
) -> list[Path]:
    if not bool((autopilot_config.get("first_frame") or {}).get("enabled", True)):
        raise AccountAutopilotError("V3 autopilot requires first_frame.enabled=true.")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AccountAutopilotError("OPENAI_API_KEY is required for first-frame generation.")
    from daily_factory.first_frames import generate_openai_first_frame
    from Mix.v3.image_qa import build_fixed_prompt

    first_frame_config = dict(autopilot_config.get("first_frame") or {})
    settings = {
        "model": str(first_frame_config.get("openai_model") or "gpt-image-2"),
        "size": str(first_frame_config.get("openai_size") or "720x1280"),
        "quality": str(first_frame_config.get("openai_quality") or "medium"),
        "timeout": int(first_frame_config.get("timeout_seconds") or 240),
        "ffmpeg_path": "ffmpeg",
    }
    character_references = load_configured_reference_files(
        account_dir,
        list(first_frame_config.get("character_reference_images") or []),
        label="character reference",
    )
    explicit = list(explicit_references or [])
    local_references = load_local_reference_images(account_dir, first_frame_config)
    aesthetic_references: list[Path] = [*explicit, *local_references]
    if bool(first_frame_config.get("with_cloudinary_refs", True)):
        aesthetic_references.extend(load_reference_images(account_dir, wrapper_config, run_dir))
    references = deduplicate_paths([*character_references, *aesthetic_references])
    generated: list[Path] = []
    for record in records:
        index = int(record["index"])
        scene_id = slug(str(record["scene_id"]))
        output_path = run_dir / "first_images" / f"{index:02d}_{scene_id}.png"
        prompt = str(record["image_prompt"])
        if character_references:
            prompt += (
                "\n\nCHARACTER LOCK: The first attached reference image is the canonical identity for this account. "
                "Preserve the same underlying person, face shape, eyes, nose, age, skin tone, hairline, and overall "
                "identity. Change only the scene-specific action, object, framing, and clothing requested by the scene."
            )
        if aesthetic_references:
            prompt += (
                "\n\nThe remaining attached images are concept-frame and visual-quality references. Use them only "
                "for composition ideas, aesthetic quality, UGC realism, "
                "lighting, framing, hand/object naturalness, and product/object texture. Do not copy identities, "
                "logos, captions, or unrelated objects from the references."
            )
        output_path.with_suffix(".prompt.txt").parent.mkdir(parents=True, exist_ok=True)
        qa_result: dict[str, Any] = {}
        for attempt in range(1, 4):
            output_path.with_suffix(f".attempt_{attempt}.prompt.txt").write_text(prompt, encoding="utf-8")
            generate_openai_first_frame(
                prompt=prompt,
                output_path=output_path,
                reference_images=references,
                api_key=api_key,
                settings=settings,
            )
            qa_result = evaluate_first_frame(
                image_path=output_path,
                qa_prompt=str(record["qa_prompt"]),
                api_key=api_key,
                model=str(first_frame_config.get("qa_model") or os.getenv("OPENAI_ANALYZER_MODEL") or "gpt-4.1-mini"),
            )
            write_json(output_path.with_suffix(f".attempt_{attempt}.qa.json"), qa_result)
            if first_frame_passed(qa_result):
                break
            prompt = build_fixed_prompt(
                prompt,
                qa_result,
                project={
                    "subject_label": record.get("subject_label"),
                    "subject_placement_hint": record.get("subject_placement_hint"),
                },
            )
        else:
            raise AccountAutopilotError(
                f"First-frame QA failed after 3 attempts for scene {record['scene_id']}: "
                f"{qa_result.get('issues') or 'unspecified visual failure'}"
            )
        output_path.with_suffix(".prompt.txt").write_text(prompt, encoding="utf-8")
        generated.append(output_path)
    update_v3_manifest_first_images(run_dir, generated)
    return generated


def evaluate_first_frame(*, image_path: Path, qa_prompt: str, api_key: str, model: str) -> dict[str, Any]:
    import requests

    image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": qa_prompt},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{image_data}", "detail": "high"},
                    ],
                }
            ],
            "text": {"format": {"type": "json_object"}},
        },
        timeout=180,
    )
    if response.status_code >= 400:
        raise AccountAutopilotError(
            f"First-frame QA request failed: HTTP {response.status_code}: {response.text[:800]}"
        )
    payload = response.json()
    text = str(payload.get("output_text") or "").strip()
    if not text:
        parts: list[str] = []
        for item in list(payload.get("output") or []):
            for content in list(item.get("content") or []):
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        text = "\n".join(parts).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AccountAutopilotError(f"First-frame QA returned invalid JSON: {text[:800]}") from exc
    if not isinstance(parsed, dict):
        raise AccountAutopilotError("First-frame QA response must be an object.")
    parsed["model"] = model
    return parsed


def first_frame_passed(result: dict[str, Any]) -> bool:
    if str(result.get("status") or "").upper() != "PASS":
        return False
    for key in ("product_placement", "hand_realism", "face_quality", "background_realism", "ugc_authenticity"):
        try:
            if float(result.get(key) or 0) < 8:
                return False
        except (TypeError, ValueError):
            return False
    return not bool(result.get("issues"))


def load_configured_reference_files(account_dir: Path, values: list[Any], *, label: str) -> list[Path]:
    references: list[Path] = []
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        path = require_inside(account_dir, resolve_path(account_dir, value), label)
        if not path.exists() or not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise AccountAutopilotError(f"Invalid {label}: {path}")
        references.append(path)
    return references


def load_local_reference_images(account_dir: Path, first_frame_config: dict[str, Any]) -> list[Path]:
    raw_dir = str(first_frame_config.get("local_reference_dir") or "").strip()
    if not raw_dir:
        return []
    reference_dir = require_inside(
        account_dir,
        resolve_path(account_dir, raw_dir),
        "local reference directory",
    )
    if not reference_dir.exists() or not reference_dir.is_dir():
        raise AccountAutopilotError(f"Missing local reference directory: {reference_dir}")
    limit = max(0, int(first_frame_config.get("local_reference_max_images") or 4))
    images = sorted(
        path
        for path in reference_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    )
    if not images:
        raise AccountAutopilotError(f"No local reference images found in: {reference_dir}")
    return images[:limit] if limit else images


def deduplicate_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def load_reference_images(account_dir: Path, wrapper_config: dict[str, Any], run_dir: Path) -> list[Path]:
    from Mix.v3.cloudinary_refs import DEFAULT_REFERENCE_FOLDER, load_cloudinary_references

    folder = str(wrapper_config.get("cloudinary_reference_folder") or DEFAULT_REFERENCE_FOLDER).strip()
    count = int(wrapper_config.get("cloudinary_max_reference_images") or 4)
    ref_dir = run_dir / "cloudinary_refs" / "ugc_refs"
    try:
        return load_cloudinary_references(folder=folder, count=count, out_dir=ref_dir)
    except Exception as exc:
        cached_value = str(wrapper_config.get("cloudinary_cached_reference_dir") or "").strip()
        if not cached_value:
            raise
        cached_dir = require_inside(account_dir, resolve_path(account_dir, cached_value), "cached reference directory")
        refs = sorted(path for path in cached_dir.glob("*") if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})[:count]
        if not refs:
            raise
        ref_dir.mkdir(parents=True, exist_ok=True)
        (ref_dir / "cloudinary_fetch_warning.txt").write_text(
            f"Cloudinary live refs failed for {folder}; using cached refs from {cached_dir}.\nOriginal error: {exc}\n",
            encoding="utf-8",
        )
        return refs


def update_v3_manifest_first_images(run_dir: Path, first_images: list[Path]) -> None:
    path = run_dir / "v3_execution_manifest.json"
    manifest = read_json(path)
    manifest["generated_first_images"] = [str(path) for path in first_images]
    for record, image_path in zip(manifest.get("scenes", []), first_images):
        record["first_image"] = str(image_path)
    write_json(path, manifest)


def build_run_omni_config(
    source_config: dict[str, Any],
    first_frames: list[Path],
    run_dir: Path,
    autopilot_config: dict[str, Any],
    *,
    today: date,
    concept_id: str,
) -> Path:
    patched = copy.deepcopy(source_config)
    patched["master_prompt"] = str(autopilot_config.get("omni_master_prompt") or OMNI_MASTER_PROMPT)
    patch_reference_images(patched, first_frames)
    total_duration = creative_duration_seconds(patched)
    if total_duration > 0:
        variants = patched.setdefault("variants", {})
        variants["count"] = 1
        variants["min_total_seconds"] = max(0.1, total_duration - 0.01)
        variants["max_total_seconds"] = total_duration + 0.01
        variants["stitch_leaf_segments"] = True
    provider = patched.setdefault("google_omni_flash", dict(patched.get("provider") or {}))
    prefix = str(
        autopilot_config.get("generation_output_gcs_uri_prefix")
        or autopilot_config.get("output_gcs_uri_prefix")
        or ""
    ).rstrip("/")
    if prefix:
        provider["output_gcs_uri"] = f"{prefix}/{today.strftime('%Y%m%d')}/{slug(concept_id)}/outputs/"
    output_path = run_dir / f"{slug(concept_id)}_omni_config.json"
    write_json(output_path, patched)
    return output_path


def creative_duration_seconds(config: dict[str, Any]) -> float:
    fallback = float(dict(config.get("defaults") or {}).get("duration_seconds") or 5)
    total = 0.0
    for role in ("hooks", "mains", "meals", "ctas", "desserts", "closings"):
        for component in list(config.get(role) or []):
            if not isinstance(component, dict):
                continue
            segments = component.get("segments")
            leaves = segments if isinstance(segments, list) and segments else [component]
            for leaf in leaves:
                if isinstance(leaf, dict):
                    total += float(leaf.get("duration_seconds") or component.get("duration_seconds") or fallback)
    return round(total, 3)


def patch_reference_images(config: dict[str, Any], first_frames: list[Path]) -> None:
    paths = iter([str(path) for path in first_frames])
    for role in ("hooks", "mains", "meals", "ctas", "desserts", "closings"):
        for component in list(config.get(role) or []):
            if not isinstance(component, dict):
                continue
            segments = component.get("segments")
            if isinstance(segments, list) and segments:
                for segment in segments:
                    if isinstance(segment, dict):
                        segment["reference_images"] = [next(paths)]
                component["reference_images"] = []
            else:
                component["reference_images"] = [next(paths)]
    try:
        next(paths)
        raise AccountAutopilotError("Unused first frames remained after patching Omni config.")
    except StopIteration:
        return


def run_omni(config_path: Path, omni_dir: Path, run_dir: Path) -> None:
    command = [
        sys.executable,
        str(ROOT / "pipeline_google_omni_stack" / "omni_stack_runner.py"),
        "--config",
        str(config_path),
        "--mode",
        "all",
        "--out",
        str(omni_dir),
    ]
    run_logged(command, cwd=ROOT, timeout=7200, stdout_path=run_dir / "omni_stdout.txt", stderr_path=run_dir / "omni_stderr.txt")


def resolve_variant_video(omni_dir: Path) -> Path:
    path = omni_dir / "variants" / "variant_001" / "final_video.mp4"
    if not path.exists() or path.stat().st_size <= 0:
        raise AccountAutopilotError(f"Expected Omni final video was not created: {path}")
    return path


def postprocess_publish_ready(input_path: Path, run_dir: Path, config: dict[str, Any]) -> Path:
    from pipeline_google_omni_stack.postprocess import postprocess_video

    output_path = run_dir / "publish_ready" / "final_video_publish_ready_1080x1920_30fps.mp4"
    postprocess_video(
        input_path,
        output_path,
        preset_name=str(config.get("postprocess_preset") or "ugc_soft_30fps"),
        log_path=output_path.with_suffix(".ffmpeg_log.txt"),
        timeout_seconds=1800,
    )
    return output_path


def finish_captions_and_hook(
    input_path: Path,
    omni_config_path: Path,
    source_config: dict[str, Any],
    run_dir: Path,
) -> Path:
    from pipeline_google_omni_stack.publish_finish import finish_for_publish

    out_dir = run_dir / "publish_finish"
    hook_text = derive_visual_hook_text(source_config)
    metadata = finish_for_publish(
        input_video=input_path,
        config_path=omni_config_path,
        out_dir=out_dir,
        hook_text=hook_text,
        silence_noise="-38dB",
        silence_duration=0.35,
        keep_start=0.06,
        keep_end=0.08,
        caption_delay=0.16,
    )
    output = Path(str(metadata.get("final_video") or ""))
    if not output.exists() or output.stat().st_size <= 0:
        raise AccountAutopilotError(f"Caption/hook finish did not create a video: {output}")
    return output


def derive_visual_hook_text(source_config: dict[str, Any]) -> str:
    hooks = [item for item in list(source_config.get("hooks") or []) if isinstance(item, dict)]
    if not hooks:
        raise AccountAutopilotError("Cannot derive visual hook text without a hook scene.")
    hook = hooks[0]
    explicit = str(hook.get("hook_text") or source_config.get("hook_text") or "").strip()
    text = explicit or str(hook.get("script") or "").strip()
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    if not text:
        raise AccountAutopilotError("First hook requires hook_text or script for the visual overlay.")
    words = text.split()
    return " ".join(words[:12])


def write_video_manifest(
    account_id: str,
    run_dir: Path,
    video_path: Path,
    concept: dict[str, Any],
    publish_at: datetime,
) -> Path:
    path = run_dir / "video_manifest.json"
    write_json(
        path,
        {
            "account_id": account_id,
            "pipeline": "v3",
            "concept_id": concept.get("concept_id"),
            "video_path": str(video_path),
            "publish_at": publish_at.isoformat(),
        },
    )
    return path


def publish_metricool(
    *,
    account_id: str,
    video_path: Path,
    manifest_path: Path,
    caption: str,
    publish_at: datetime,
    platforms: str,
    dry_run: bool,
    run_dir: Path,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "social_scheduler.scheduler",
        "--account",
        account_id,
        "publish-now",
        "--video",
        str(video_path),
        "--caption",
        caption,
        "--platforms",
        platforms,
        "--publish-at",
        publish_at.isoformat(),
        "--manifest",
        str(manifest_path),
    ]
    if dry_run:
        command.append("--dry-run")
    completed = run_logged(
        command,
        cwd=ROOT,
        timeout=900,
        stdout_path=run_dir / "metricool_stdout.txt",
        stderr_path=run_dir / "metricool_stderr.txt",
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "unknown", "stdout": completed.stdout}


def select_concept(config: dict[str, Any], *, cycle_index: int, forced_id: str) -> dict[str, Any]:
    concepts = list(config.get("concepts") or [])
    if not concepts:
        raise AccountAutopilotError("autopilot_v3.json requires at least one concept.")
    if forced_id:
        for concept in concepts:
            if str(concept.get("concept_id") or "") == forced_id:
                return dict(concept)
        raise AccountAutopilotError(f"Unknown concept_id: {forced_id}")
    return dict(concepts[cycle_index % len(concepts)])


def load_generation_request(path_value: str, *, expected_account_id: str) -> dict[str, Any]:
    if not path_value.strip():
        return {}
    path = Path(path_value).resolve()
    request = read_json(path)
    account_id = normalize_account_id(str(request.get("account_id") or ""))
    if account_id != expected_account_id:
        raise AccountAutopilotError(
            f"Dashboard request account_id={account_id} does not match selected account {expected_account_id}."
        )
    source = request.get("source_config")
    if not isinstance(source, dict):
        raise AccountAutopilotError("Dashboard request requires source_config object.")
    source_account = normalize_account_id(str(source.get("account_id") or ""))
    if source_account != expected_account_id:
        raise AccountAutopilotError("Dashboard source_config belongs to a different account.")
    raw_concept_id = str(request.get("concept_id") or source.get("concept_id") or "").strip()
    if not raw_concept_id:
        raise AccountAutopilotError("Dashboard request requires concept_id.")
    concept_id = slug(raw_concept_id)
    for key in ("hooks", "mains", "ctas"):
        if not isinstance(source.get(key), list) or not source[key]:
            raise AccountAutopilotError(f"Dashboard source_config requires non-empty {key}.")
    attachments = list(request.get("reference_attachments") or [])
    if len(attachments) > 20:
        raise AccountAutopilotError("Dashboard request supports at most 20 reference attachments.")
    for item in attachments:
        if not isinstance(item, dict):
            raise AccountAutopilotError("Dashboard reference attachment must be an object.")
        if normalize_account_id(str(item.get("account_id") or "")) != expected_account_id:
            raise AccountAutopilotError("Dashboard reference attachment belongs to a different account.")
        if not str(item.get("storage_uri") or "").startswith("gs://"):
            raise AccountAutopilotError("Dashboard reference attachment must use a Cloud Storage URI.")
    cloud_route = dict(request.get("cloud_route") or {})
    if cloud_route:
        if normalize_account_id(str(cloud_route.get("account_id") or "")) != expected_account_id:
            raise AccountAutopilotError("Dashboard Google Cloud route belongs to a different account.")
        for key in ("master_output_gcs_uri", "result_gcs_uri"):
            if not str(cloud_route.get(key) or "").startswith("gs://"):
                raise AccountAutopilotError(f"Dashboard Google Cloud route requires {key}.")
    return {
        **request,
        "account_id": account_id,
        "concept_id": concept_id,
        "source_config": source,
        "reference_attachments": attachments,
        "cloud_route": cloud_route,
    }


def is_due(today: date, *, start_date: date, interval_days: int) -> bool:
    if today < start_date:
        return False
    return (today - start_date).days % max(1, interval_days) == 0


def load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "America/Mexico_City":
            return timezone(timedelta(hours=-6), name)
        raise


def scheduled_publish_datetime(today: date, tz: tzinfo, value: str) -> datetime:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return datetime.combine(today, dt_time(hour=hour, minute=minute), tzinfo=tz)


def resolve_publish_datetime(value: str, *, today: date, tz: tzinfo, default_time: str) -> datetime:
    raw = value.strip()
    if not raw:
        return scheduled_publish_datetime(today, tz, default_time)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AccountAutopilotError("--publish-at must be a valid ISO-8601 datetime.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def resolve_autopilot_path(account_id: str, account_dir: Path, registry_entry: dict[str, Any] | None) -> Path:
    value = str((registry_entry or {}).get("autopilot_config") or "autopilot_v3.json").strip()
    if not value:
        value = "autopilot_v3.json"
    path = resolve_path(ROOT, value) if value.startswith("accounts/") else resolve_path(account_dir, value)
    return require_inside(account_dir, path, "autopilot config")


def load_account_secret_bundle(account_id: str) -> None:
    raw = os.environ.get("ACCOUNT_PUBLISH_SECRETS_JSON", "").strip()
    if not raw:
        return
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AccountAutopilotError("ACCOUNT_PUBLISH_SECRETS_JSON is not valid JSON.") from exc
    if not isinstance(bundle, dict):
        raise AccountAutopilotError("ACCOUNT_PUBLISH_SECRETS_JSON must be a JSON object.")
    prefix = account_id.upper()
    candidates = [
        account_id,
        account_id.replace("_", "-"),
        prefix,
        prefix.replace("_", "-"),
    ]
    raw_secrets: Any = None
    for key in candidates:
        if key in bundle:
            raw_secrets = bundle[key]
            break
    if raw_secrets is None:
        return
    if not isinstance(raw_secrets, dict):
        raise AccountAutopilotError(f"Secret bundle entry for {account_id} must be an object.")
    for key, value in raw_secrets.items():
        env_key = str(key).strip().upper()
        if not env_key:
            continue
        if not env_key.startswith(prefix + "_"):
            env_key = f"{prefix}_{env_key}"
        os.environ[env_key] = str(value)


def assert_account_id(config: dict[str, Any], account_id: str, label: str) -> None:
    declared = normalize_account_id(str(config.get("account_id") or ""))
    if declared != account_id:
        raise AccountAutopilotError(f"{label} declares account_id={declared}, expected {account_id}.")


def require_inside(root: Path, path: Path, label: str) -> Path:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise AccountAutopilotError(f"{label} must stay inside {root_resolved}: {path_resolved}") from exc
    return path_resolved


def normalize_account_id(account_id: str) -> str:
    value = str(account_id or "").strip().lower().replace("-", "_")
    if not value:
        raise AccountAutopilotError("Account id is required.")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_")
    if any(char not in allowed for char in value) or value.startswith("_") or value.endswith("_"):
        raise AccountAutopilotError(f"Invalid account id: {account_id}")
    return value


def run_logged(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    print("Running:", " ".join(command))
    result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        detail = safe_subprocess_detail(result.stderr)
        if detail:
            print(f"Provider failure: {detail}", file=sys.stderr)
        raise CommandExecutionError(
            result.returncode,
            classify_command_failure(result.stderr),
            detail,
        )
    elapsed = round(time.monotonic() - started, 3)
    print(f"Finished in {elapsed}s")
    return result


def classify_command_failure(stderr: str) -> str:
    value = str(stderr or "")
    lowered = value.lower()
    patterns = (
        ("responsible_ai_filtered", ("responsible_ai_filtered", "responsible ai", "filtered out")),
        ("provider_generation_failed", ("provider_generation_failed", "provider generation failed")),
        (
            "authentication_failed",
            (
                "unauthenticated",
                "invalid authentication credentials",
                "missing google auth token",
                "application default credentials access token",
            ),
        ),
        ("permission_denied", ("permission denied", "permissiondenied", "status 403", "http 403")),
        ("resource_not_found", ("notfound", "status 404", "http 404", "was not found")),
        ("rate_limited", ("resourceexhausted", "rate limit", "status 429", "http 429")),
        ("provider_timeout", ("deadlineexceeded", "timed out", "timeout")),
        ("invalid_request", ("invalidargument", "status 400", "http 400")),
        ("ssl_error", ("sslerror", "certificate verify failed")),
    )
    for code, needles in patterns:
        if any(needle in lowered for needle in needles):
            return code
    exception_names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\s*:", value)
    if exception_names:
        return exception_names[-1]
    return "subprocess_failed"


def safe_subprocess_detail(stderr: str, *, max_length: int = 600) -> str:
    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    if not lines:
        return "subprocess returned no stderr; inspect the uploaded provider logs"
    detail = lines[-1]
    redactions = (
        (r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+", r"\1[REDACTED]"),
        (r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]"),
        (r"(?i)([?&](?:key|token|api_key)=)[^&\s]+", r"\1[REDACTED]"),
    )
    for pattern, replacement in redactions:
        detail = re.sub(pattern, replacement, detail)
    return detail[:max_length]


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise AccountAutopilotError(f"Expected object JSON: {path}")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_env(path: Path, *, override: bool = False) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def slug(value: str) -> str:
    chars: list[str] = []
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
        elif chars and chars[-1] != "_":
            chars.append("_")
    return "".join(chars).strip("_") or "item"


if __name__ == "__main__":
    raise SystemExit(main())
