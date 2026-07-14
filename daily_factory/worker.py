from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .characters import (
    CharacterProfile,
    character_text,
    load_character,
    require_generation_ready,
    require_publishing_ready,
)
from .first_frames import prepare_omni_v11_first_frames


ROOT = Path(__file__).resolve().parent.parent


class DailyFactoryError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and publish one daily UGC Reel from the stable Omni stack.")
    parser.add_argument("--config", default=str(ROOT / "daily_factory" / "config.json"))
    parser.add_argument("--queue", default=str(ROOT / "daily_factory" / "content_queue.json"))
    parser.add_argument("--date", default="", help="Local YYYY-MM-DD. Defaults to today in configured timezone.")
    parser.add_argument("--concept-id", default="", help="Generate a specific concept id instead of the scheduled daily concept.")
    parser.add_argument("--dry-run", action="store_true", help="Build config and manifest without generation or publishing.")
    parser.add_argument("--force", action="store_true", help="Ignore an existing successful daily manifest.")
    parser.add_argument("--no-publish", action="store_true", help="Generate the final video but skip Instagram/Facebook publishing.")
    parser.add_argument("--out", default="", help="Optional working directory; defaults to a temporary directory.")
    args = parser.parse_args(argv)

    load_env(ROOT / ".env.local")
    config_path = Path(args.config).resolve()
    config = read_json(config_path)
    queue = read_json(Path(args.queue).resolve())
    local_timezone = resolve_timezone(str(config.get("timezone") or "America/Mexico_City"))
    run_date = args.date or datetime.now(local_timezone).date().isoformat()
    concept = select_concept(queue, run_date, args.concept_id)
    character = load_character(ROOT, config, concept)
    require_generation_ready(character)
    release = str(os.environ.get("DAILY_FACTORY_RELEASE") or config.get("release") or "v1")
    dry_run = args.dry_run or env_bool("DAILY_FACTORY_DRY_RUN", False)
    bucket = str(config["bucket"])
    prefix = f"{str(config.get('output_prefix') or 'daily-factory').strip('/')}/{run_date}"
    token = "" if dry_run else google_access_token()

    existing = None if dry_run else read_gcs_json(bucket, f"{prefix}/manifest.json", token)
    if existing and existing.get("status") == "published" and not args.force:
        print(json.dumps({"status": "already_published", "manifest": existing}, indent=2))
        return 0

    work_dir = Path(args.out).resolve() if args.out else Path(tempfile.mkdtemp(prefix=f"daily-factory-{run_date}-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    generated_config = build_omni_config(config, concept, run_date, work_dir, character=character)
    if use_omni_v11(config) and not dry_run:
        generated_config = prepare_omni_v11_first_frames(generated_config, config, work_dir=work_dir, root=ROOT)
    elif use_omni_v11(config):
        generated_config["recipe_id"] = "daily_factory_v1_1"
        generated_config["first_frame_source"] = {
            "provider": "openai_images",
            "mode": "dry_run_not_generated",
        }
    generated_config_path = work_dir / "omni_config.json"
    write_json(generated_config_path, generated_config)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "release": release,
        "date": run_date,
        "concept_id": concept["id"],
        "character_id": character.character_id,
        "status": "dry_run" if dry_run else "generating",
        "created_at": utc_now(),
        "work_dir": str(work_dir),
        "platforms": {},
    }
    write_json(work_dir / "manifest.json", manifest)
    if dry_run:
        if os.environ.get("CLOUD_RUN_JOB"):
            cloud_token = google_access_token()
            read_gcs_json(bucket, f"{prefix}/dry-run-credential-probe.json", cloud_token)
            required_secrets = publishing_secret_envs(character) if bool(config.get("publish", True)) else []
            if use_omni_v11(config):
                required_secrets.append(str(config.get("openai_api_key_env") or "OPENAI_API_KEY"))
            for secret_name in required_secrets:
                if not os.environ.get(secret_name, "").strip():
                    raise DailyFactoryError(f"Cloud secret is missing: {secret_name}")
            probe_object = f"{prefix}/dry-run-public-delivery-probe.json"
            upload_gcs_bytes(bucket, probe_object, b'{"ok":true}\n', "application/json", cloud_token)
            make_gcs_object_public(bucket, probe_object, cloud_token)
            with urllib.request.urlopen(public_gcs_url(bucket, probe_object), timeout=30) as response:
                if response.status != 200:
                    raise DailyFactoryError(f"Public GCS probe returned HTTP {response.status}.")
            delete_gcs_object(bucket, probe_object, cloud_token)
            manifest["cloud_credentials_verified"] = True
            manifest["public_delivery_verified"] = True
            write_json(work_dir / "manifest.json", manifest)
        run_command([
            sys.executable,
            str(ROOT / "pipeline_google_omni_stack" / "omni_stack_runner.py"),
            "--config", str(generated_config_path),
            "--mode", "dry-run",
            "--out", str(work_dir / "generation"),
        ], work_dir / "dry_run.log", timeout=180)
        print(json.dumps(manifest, indent=2))
        return 0

    try:
        acquire_lock(bucket, f"{prefix}/lock.json", token, {"release": release, "created_at": utc_now()}, force=args.force)
        generation_dir = work_dir / "generation"
        run_command([
            sys.executable,
            str(ROOT / "pipeline_google_omni_stack" / "omni_stack_runner.py"),
            "--config", str(generated_config_path),
            "--mode", "all",
            "--out", str(generation_dir),
        ], work_dir / "generation.log", timeout=5400)
        assembled = generation_dir / "variants" / "variant_001" / "final_video.mp4"
        require_file(assembled)
        finish_dir = work_dir / "publish"
        run_command([
            sys.executable,
            str(ROOT / "pipeline_google_omni_stack" / "publish_finish.py"),
            "--input", str(assembled),
            "--config", str(generated_config_path),
            "--out", str(finish_dir),
            "--hook-text", str(concept["hook_overlay"]),
        ], work_dir / "publish_finish.log", timeout=1200)
        final_video = finish_dir / "final_video_publish_ready.mp4"
        require_file(final_video)
        video_object = f"{prefix}/{release}/final_video.mp4"
        upload_gcs_bytes(bucket, video_object, final_video.read_bytes(), "video/mp4", token)
        make_gcs_object_public(bucket, video_object, token)
        public_url = public_gcs_url(bucket, video_object)
        manifest.update({
            "status": "generated",
            "final_video": str(final_video),
            "gcs_uri": f"gs://{bucket}/{video_object}",
            "public_video_url": public_url,
            "generated_at": utc_now(),
        })
        write_json(work_dir / "manifest.json", manifest)
        upload_gcs_json(bucket, f"{prefix}/manifest.json", manifest, token)

        if bool(config.get("publish", True)) and not args.no_publish:
            manifest["platforms"] = publish_platforms(config, character, final_video, public_url, str(concept["caption"]))
            failed = [name for name, result in manifest["platforms"].items() if result.get("status") == "failed"]
            if failed:
                raise DailyFactoryError("Publishing failed for: " + ", ".join(failed))
            manifest["status"] = "published"
            manifest["published_at"] = utc_now()
        else:
            manifest["status"] = "generated"
            manifest["publishing_skipped"] = True
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        manifest["failed_at"] = utc_now()
        write_json(work_dir / "manifest.json", manifest)
        upload_gcs_json(bucket, f"{prefix}/manifest.json", manifest, token)
        delete_gcs_object(bucket, f"{prefix}/lock.json", token)
        raise

    write_json(work_dir / "manifest.json", manifest)
    upload_gcs_json(bucket, f"{prefix}/manifest.json", manifest, token)
    upload_gcs_bytes(bucket, f"{prefix}/{release}/run_manifest.json", json_bytes(manifest), "application/json", token)
    print(json.dumps(manifest, indent=2))
    return 0


def select_concept(queue: dict[str, Any], run_date: str, concept_id: str = "") -> dict[str, Any]:
    concepts = list(queue.get("concepts") or [])
    if not concepts:
        raise DailyFactoryError("The daily content queue is empty.")
    if concept_id:
        for concept in concepts:
            if str(concept.get("id") or "") == concept_id:
                return dict(concept)
        raise DailyFactoryError(f"Unknown concept id: {concept_id}")
    ordinal = datetime.strptime(run_date, "%Y-%m-%d").date().toordinal()
    return dict(concepts[ordinal % len(concepts)])


def build_omni_config(
    config: dict[str, Any],
    concept: dict[str, Any],
    run_date: str,
    work_dir: Path,
    *,
    character: CharacterProfile | None = None,
) -> dict[str, Any]:
    character = character or load_character(ROOT, config, concept)
    require_generation_ready(character)
    reference = character.master_reference
    project_id = str(config["project_id"])
    bucket = str(config["bucket"])
    cta_keyword = str(concept.get("cta_keyword") or "LABEL").strip().upper()
    segments = []
    raw_segments = list(concept.get("segments") or [])
    if not raw_segments:
        raise DailyFactoryError(f"Concept {concept.get('id')} has no segments.")
    for index, raw_segment in enumerate(raw_segments, start=1):
        segment = normalize_segment(raw_segment, index)
        generated_segment = {
            "id": segment["id"],
            "duration_seconds": segment["duration_seconds"],
            "prompt": scene_prompt(
                visual=segment["visual"],
                dialogue=segment["dialogue"],
                concept=concept,
                role=f"main scene {index}",
                character=character,
            ),
        }
        if "reference_images" in segment:
            generated_segment["reference_images"] = segment["reference_images"]
        segments.append(generated_segment)
    return {
        "name": f"Daily {character.name} Reel {run_date}",
        "concept_id": concept["id"],
        "concept": {
            "id": concept["id"],
            "character_id": character.character_id,
        },
        "character": character_payload(character),
        "recipe_id": str(config.get("recipe_id") or "daily_factory_v1"),
        "google_omni_flash": {
            "model": str(config.get("omni_model") or "gemini-omni-flash-preview"),
            "project_id": project_id,
            "location": str(config.get("omni_location") or "global"),
            "access_token_env": "GOOGLE_OAUTH_ACCESS_TOKEN",
            "output_gcs_uri": f"gs://{bucket}/daily-factory/{run_date}/provider/",
            "poll_seconds": 10,
            "timeout_seconds": 1200,
            "retries": 2,
            "background": True,
        },
        "defaults": {"aspect_ratio": "9:16", "duration_seconds": 8, "resolution": "720p"},
        "master_prompt": master_prompt(character),
        "hooks": [{
            "id": "daily_hook",
            "title": concept["id"],
            "duration_seconds": int(concept.get("hook_duration_seconds") or 6),
            "reference_images": [reference],
            "prompt": scene_prompt(
                visual=str(
                    concept.get("hook_visual")
                    or f"Single continuous supermarket selfie shot. {character.name} holds only the named supermarket product for this concept close to the camera, then lowers it slightly while maintaining direct eye contact."
                ),
                dialogue=str(concept["hook"]),
                concept=concept,
                role="hook",
                character=character,
            ),
        }],
        "meals": [{
            "id": "daily_main",
            "title": concept["id"],
            "reference_images": [reference],
            "prompt": "Each segment is generated as its own independent leaf scene. One location, one physical action, one exact spoken line per segment.",
            "segments": segments,
        }],
        "ctas": [{
            "id": "daily_cta",
            "title": f"Comment {cta_keyword}",
            "duration_seconds": int(concept.get("cta_duration_seconds") or 6),
            "reference_images": [reference],
            "prompt": scene_prompt(
                visual=str(
                    concept.get("cta_visual")
                    or f"Single continuous kitchen selfie shot. {character.name} points down once with direct eye contact. No card and no generated text."
                ),
                dialogue=str(
                    concept.get("cta")
                    or f"Comment {cta_keyword} and I'll send you the three things I check before a product goes in my cart."
                ),
                concept=concept,
                role="CTA",
                character=character,
            ),
        }],
        "variants": variant_settings(concept, run_date, segments),
    }


def character_payload(character: CharacterProfile) -> dict[str, str]:
    return {
        "character_id": character.character_id,
        "name": character.name,
        "master_reference": character.master_reference,
        "metadata_path": character.metadata_path,
        "identity": character.prompt_identity(),
        "voice": character.voice,
    }


def master_prompt(character: CharacterProfile) -> str:
    return (
        "GOOGLE OMNI MASTER PROMPT. Create ONE vertical 9:16 realistic UGC smartphone clip with native spoken audio. "
        "This is one leaf scene, never a montage or multi-scene story. Never combine multiple scenes inside one generated clip. "
        f"Selected character: {character.prompt_identity()} "
        f"When a {character.name} reference image is supplied, use it as the fixed identity anchor and preserve the same face, age, eyes, skin texture, hair, and ordinary appearance. "
        f"For product-only B-roll without a reference image, do not show a face. {character.outfit or 'Use the selected character outfit'} when {character.name} is visible. One continuous handheld iPhone-style shot. "
        "Use only the named environment and objects for this exact scene. Do not carry objects from earlier scenes into later scenes unless named again. "
        "Objects remain solid, correctly gripped, and physically realistic. "
        "Natural restrained movement, direct eye contact while speaking, no floating, sliding, morphing, duplicate products, jump cuts, location changes, "
        "cinematic movement, beauty filter, text, subtitles, logos, social UI, or extra dialogue. Start speaking immediately and finish the full line. "
        f"No unfinished sentence, no repeated words, no repeated opening phrase, and no trailing self-interruption. {character.name} is a person, never a product brand. "
        "Use real supermarket product cues and named real supermarket products when provided. Never invent creator-branded products. "
        "Do not render prompt-control wording or the creator name on packaging. Say only the exact Native dialogue once."
    )


def normalize_segment(raw_segment: Any, index: int) -> dict[str, Any]:
    fallback_visuals = [
        "Single continuous supermarket aisle close shot. Claire holds only the named product for this concept and keeps it physically stable.",
        "Single continuous over-shoulder label check. Claire points near the label while the product stays in her hand.",
        "Single continuous lived-in kitchen counter shot. Claire lays out only the named whole-food swap ingredients.",
        "Single continuous close food-preparation shot. Claire completes one realistic action with normal hand and object physics.",
    ]
    if isinstance(raw_segment, dict):
        dialogue = str(raw_segment.get("dialogue") or raw_segment.get("text") or "").strip()
        visual = str(raw_segment.get("visual") or raw_segment.get("prompt") or fallback_visuals[(index - 1) % len(fallback_visuals)]).strip()
        sid = str(raw_segment.get("id") or f"main_{index:02d}").strip()
        duration = int(raw_segment.get("duration_seconds") or 8)
        references = raw_segment.get("reference_images") if "reference_images" in raw_segment else None
    else:
        dialogue = str(raw_segment).strip()
        visual = fallback_visuals[(index - 1) % len(fallback_visuals)]
        sid = f"main_{index:02d}"
        duration = 8
        references = None
    if not dialogue:
        raise DailyFactoryError(f"Segment {index} has no dialogue.")
    if not dialogue.endswith((".", "!", "?")):
        dialogue += "."
    normalized = {"id": sid, "visual": visual, "dialogue": dialogue, "duration_seconds": duration}
    if references is not None:
        if not isinstance(references, list):
            raise DailyFactoryError(f"Segment {index} reference_images must be a list.")
        normalized["reference_images"] = [str(item) for item in references]
    return normalized


def scene_prompt(*, visual: str, dialogue: str, concept: dict[str, Any], role: str, character: CharacterProfile) -> str:
    forbidden = str(
        concept.get("forbidden_objects")
        or "unrelated fruit, juice bottles, salad dressing, salad greens, ebook covers, creator-branded products, fake wellness jars, extra CTA cards"
    )
    visual = character_text(visual, character)
    dialogue = character_text(dialogue, character)
    return (
        f"{visual.strip()}\n"
        f"Scene role: {role}. This is one complete leaf clip only.\n"
        f"Object lock: use only the objects named in this scene. Forbidden unless explicitly named in this scene: {forbidden}.\n"
        "Product lock: products must look like real supermarket items from actual stores. If a known brand is named, use that brand cue naturally; "
        f"do not replace it with a made-up {character.name} brand, do not print prompt-control wording on the package, and do not invent fake readable claims.\n"
        "Physics lock: no floating, sliding, warping, duplicate packages, extra fingers, hands passing through objects, or utensils buried inside jars.\n"
        "Continuity lock: the spoken line starts cleanly, ends as a complete sentence, and is not repeated.\n"
        f"Native dialogue: {dialogue.strip()}"
    )


def variant_settings(concept: dict[str, Any], run_date: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    hook_seconds = int(concept.get("hook_duration_seconds") or 6)
    cta_seconds = int(concept.get("cta_duration_seconds") or 6)
    total = hook_seconds + cta_seconds + sum(int(segment["duration_seconds"]) for segment in segments)
    return {
        "count": 1,
        "min_total_seconds": max(20, total - 8),
        "max_total_seconds": total + 10,
        "seed": int(run_date.replace("-", "")),
        "stitch_leaf_segments": True,
    }


def use_omni_v11(config: dict[str, Any]) -> bool:
    recipe = str(config.get("recipe_id") or "").strip().lower()
    if recipe in {"daily_factory_v1_1", "omni_v1_1", "google_omni_v1_1"}:
        return True
    return env_bool("DAILY_FACTORY_OMNI_V11", False)


def publish_platforms(
    config: dict[str, Any],
    character: CharacterProfile,
    video: Path,
    public_url: str,
    caption: str,
) -> dict[str, Any]:
    from social_scheduler.meta_graph import MetaGraphClient, load_config

    require_publishing_ready(character)
    meta_raw = read_json(ROOT / character.meta_config)
    client = MetaGraphClient(load_config(meta_raw))
    results: dict[str, Any] = {}
    for platform in config.get("platforms") or ["instagram", "facebook"]:
        try:
            if platform == "instagram":
                result = client.publish_instagram_reel(video_url=public_url, caption=caption, share_to_feed=True)
            elif platform == "facebook":
                result = client.publish_facebook_reel(video_path=video, video_url=public_url, caption=caption)
            else:
                raise DailyFactoryError(f"Unsupported platform: {platform}")
            results[platform] = {"status": "published", **result}
        except Exception as exc:
            results[platform] = {"status": "failed", "error": str(exc)}
    return results


def publishing_secret_envs(character: CharacterProfile) -> list[str]:
    require_publishing_ready(character)
    meta_raw = read_json(ROOT / character.meta_config)
    meta = dict(meta_raw.get("meta") or meta_raw)
    return [
        str(meta.get("instagram_access_token_env") or "INSTAGRAM_ACCESS_TOKEN").strip(),
        str(meta.get("facebook_access_token_env") or "FACEBOOK_PAGE_ACCESS_TOKEN").strip(),
    ]


def google_access_token() -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return str(json.loads(response.read().decode("utf-8"))["access_token"])
    except Exception:
        command = ["gcloud.cmd" if os.name == "nt" else "gcloud", "auth", "print-access-token"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise DailyFactoryError("No Google access token is available from Cloud metadata or gcloud.")


def acquire_lock(bucket: str, object_name: str, token: str, payload: dict[str, Any], *, force: bool) -> None:
    if force:
        return
    try:
        upload_gcs_bytes(bucket, object_name, json_bytes(payload), "application/json", token, if_generation_match=0)
    except DailyFactoryError as exc:
        if "HTTP 412" in str(exc):
            raise DailyFactoryError(f"A daily run already holds the lock: gs://{bucket}/{object_name}") from exc
        raise


def upload_gcs_json(bucket: str, object_name: str, payload: dict[str, Any], token: str) -> None:
    upload_gcs_bytes(bucket, object_name, json_bytes(payload), "application/json", token)


def upload_gcs_bytes(bucket: str, object_name: str, payload: bytes, content_type: str, token: str, if_generation_match: int | None = None) -> None:
    params: dict[str, str] = {"uploadType": "media", "name": object_name}
    if if_generation_match is not None:
        params["ifGenerationMatch"] = str(if_generation_match)
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{urllib.parse.quote(bucket)}/o?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, data=payload, method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": content_type})
    open_json(request, timeout=600)


def read_gcs_json(bucket: str, object_name: str, token: str) -> dict[str, Any] | None:
    url = f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket)}/o/{urllib.parse.quote(object_name, safe='')}?alt=media"
    request = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {token}"})
    try:
        return open_json(request, timeout=60)
    except DailyFactoryError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


def make_gcs_object_public(bucket: str, object_name: str, token: str) -> None:
    url = (
        f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket)}/o/"
        f"{urllib.parse.quote(object_name, safe='')}/acl"
    )
    request = urllib.request.Request(
        url,
        data=json_bytes({"entity": "allUsers", "role": "READER"}),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    open_json(request, timeout=60)


def delete_gcs_object(bucket: str, object_name: str, token: str) -> None:
    url = f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket)}/o/{urllib.parse.quote(object_name, safe='')}"
    request = urllib.request.Request(url, method="DELETE", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60):
            return
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        body = exc.read().decode("utf-8", errors="replace")
        raise DailyFactoryError(f"Could not release GCS lock (HTTP {exc.code}): {body[:1000]}") from exc


def open_json(request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise DailyFactoryError(f"HTTP {exc.code} for {request.full_url}: {body[:2000]}") from exc


def public_gcs_url(bucket: str, object_name: str) -> str:
    return f"https://storage.googleapis.com/{urllib.parse.quote(bucket)}/{urllib.parse.quote(object_name, safe='/')}"


def run_command(command: list[str], log_path: Path, *, timeout: int) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print("Running: " + subprocess.list2cmdline(command), flush=True)
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL, cwd=ROOT)
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(f"TIMEOUT after {timeout}s\n{exc.stdout or ''}\n{exc.stderr or ''}", encoding="utf-8")
        raise DailyFactoryError(f"Command timed out after {timeout}s: {command[0]}") from exc
    log_path.write_text(f"COMMAND: {subprocess.list2cmdline(command)}\nSECONDS: {time.monotonic() - started:.2f}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}", encoding="utf-8")
    if result.returncode != 0:
        raise DailyFactoryError(f"Command failed ({result.returncode}); see {log_path}")


def require_file(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise DailyFactoryError(f"Expected output is missing: {path}")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "America/Mexico_City":
            return timezone(timedelta(hours=-6), name="America/Mexico_City")
        raise DailyFactoryError(f"Time zone data is unavailable for {name}.")


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Daily factory failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
