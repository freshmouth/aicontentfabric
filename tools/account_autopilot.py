from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    parser.add_argument("--concept-id", default="", help="Force a concept_id for a single-account run.")
    parser.add_argument("--plan-only", action="store_true", help="Validate cadence/config without provider calls.")
    parser.add_argument("--dry-run", action="store_true", help="Generate assets but dry-run Metricool publishing.")
    parser.add_argument("--skip-publish", action="store_true", help="Generate video only.")
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
    autopilot_path = resolve_autopilot_path(account_id, account_dir, registry_entry)
    if not autopilot_path.exists():
        raise AccountAutopilotError(f"Missing autopilot config for {account_id}: {autopilot_path}")
    autopilot_config = read_json(autopilot_path)
    assert_account_id(autopilot_config, account_id, "autopilot_v3.json")
    return run_autopilot(account_id, account_dir, autopilot_path, autopilot_config, args)


def run_autopilot(
    account_id: str,
    account_dir: Path,
    autopilot_path: Path,
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    if not bool(config.get("enabled", True)):
        return {"status": "disabled", "account_id": account_id}

    tz_name = str(config.get("timezone") or "America/Mexico_City")
    tz = load_timezone(tz_name)
    today = date.fromisoformat(args.today) if args.today else datetime.now(tz).date()
    start_date = date.fromisoformat(str(config.get("start_date") or today.isoformat()))
    interval_days = int(config.get("interval_days") or 1)
    due = is_due(today, start_date=start_date, interval_days=interval_days)
    cycle_index = max(0, (today - start_date).days // max(1, interval_days))
    concept = select_concept(config, cycle_index=cycle_index, forced_id=str(args.concept_id or ""))
    publish_at = scheduled_publish_datetime(today, tz, str(config.get("publish_time") or "12:00"))
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
    }
    if args.plan_only:
        plan["plan_only"] = True
        return plan
    if not due and not args.force:
        return plan

    run_id = f"autopilot_{today.strftime('%Y%m%d')}_{slug(concept['concept_id'])}"
    run_dir = account_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "autopilot_plan.json", plan)

    wrapper_path = require_inside(account_dir, resolve_path(account_dir, str(concept["v3_config"])), "V3 wrapper")
    wrapper_config = read_json(wrapper_path)
    source_config_path = require_inside(
        account_dir,
        resolve_path(wrapper_path.parent, str(wrapper_config["source_config"])),
        "V3 source config",
    )
    source_config = read_json(source_config_path)
    assert_account_id(source_config, account_id, source_config_path.name)

    v3_manifest = prepare_v3_manifest(account_id, wrapper_config, source_config, source_config_path, run_dir)
    first_frames = generate_first_frames(account_dir, wrapper_config, v3_manifest, run_dir, config)
    omni_config_path = build_run_omni_config(
        source_config,
        first_frames,
        run_dir,
        config,
        today=today,
        concept_id=str(concept["concept_id"]),
    )
    omni_dir = run_dir / "omni"
    run_omni(omni_config_path, omni_dir, run_dir)
    final_video = resolve_variant_video(omni_dir)
    publish_ready = postprocess_publish_ready(final_video, run_dir, config)
    video_manifest = write_video_manifest(account_id, run_dir, publish_ready, concept, publish_at)

    publish_result: dict[str, Any] = {"status": "skipped"}
    if not args.skip_publish:
        publish_result = publish_metricool(
            account_id=account_id,
            video_path=publish_ready,
            manifest_path=video_manifest,
            caption=str(concept["caption"]),
            publish_at=publish_at,
            platforms=str(config.get("platforms") or "instagram,facebook"),
            dry_run=bool(args.dry_run),
            run_dir=run_dir,
        )

    result = {
        **plan,
        "status": "succeeded",
        "run_dir": str(run_dir),
        "v3_manifest": str(run_dir / "v3_execution_manifest.json"),
        "omni_config": str(omni_config_path),
        "final_video": str(final_video),
        "publish_ready": str(publish_ready),
        "video_manifest": str(video_manifest),
        "metricool": publish_result,
    }
    write_json(run_dir / "autopilot_result.json", result)
    return result


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
) -> list[Path]:
    if not bool((autopilot_config.get("first_frame") or {}).get("enabled", True)):
        raise AccountAutopilotError("V3 autopilot requires first_frame.enabled=true.")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AccountAutopilotError("OPENAI_API_KEY is required for first-frame generation.")
    from daily_factory.first_frames import generate_openai_first_frame

    first_frame_config = dict(autopilot_config.get("first_frame") or {})
    settings = {
        "model": str(first_frame_config.get("openai_model") or "gpt-image-2"),
        "size": str(first_frame_config.get("openai_size") or "720x1280"),
        "quality": str(first_frame_config.get("openai_quality") or "medium"),
        "timeout": int(first_frame_config.get("timeout_seconds") or 240),
        "ffmpeg_path": "ffmpeg",
    }
    references: list[Path] = []
    if bool(first_frame_config.get("with_cloudinary_refs", True)):
        references = load_reference_images(account_dir, wrapper_config, run_dir)
    generated: list[Path] = []
    for record in records:
        index = int(record["index"])
        scene_id = slug(str(record["scene_id"]))
        output_path = run_dir / "first_images" / f"{index:02d}_{scene_id}.png"
        prompt = str(record["image_prompt"])
        if references:
            prompt += (
                "\n\nVisual quality references are attached. Use them only for aesthetic quality, UGC realism, "
                "lighting, framing, hand/object naturalness, and product/object texture. Do not copy identities, "
                "logos, captions, or unrelated objects from the references."
            )
        output_path.with_suffix(".prompt.txt").parent.mkdir(parents=True, exist_ok=True)
        output_path.with_suffix(".prompt.txt").write_text(prompt, encoding="utf-8")
        generate_openai_first_frame(
            prompt=prompt,
            output_path=output_path,
            reference_images=references,
            api_key=api_key,
            settings=settings,
        )
        generated.append(output_path)
    update_v3_manifest_first_images(run_dir, generated)
    return generated


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
    provider = patched.setdefault("google_omni_flash", dict(patched.get("provider") or {}))
    prefix = str(autopilot_config.get("output_gcs_uri_prefix") or "").rstrip("/")
    if prefix:
        provider["output_gcs_uri"] = f"{prefix}/{today.strftime('%Y%m%d')}/{slug(concept_id)}/outputs/"
    output_path = run_dir / f"{slug(concept_id)}_omni_config.json"
    write_json(output_path, patched)
    return output_path


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
        raise AccountAutopilotError(f"Command failed with exit code {result.returncode}. See {stderr_path}")
    elapsed = round(time.monotonic() - started, 3)
    print(f"Finished in {elapsed}s")
    return result


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
