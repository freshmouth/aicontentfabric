from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from adapters.google_omni_flash import GoogleOmniSettings, generate_clip
from postprocess import postprocess_video


PIPELINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = PIPELINE_DIR.parent
PRODUCT_BRANDING_RULE = (
    "Product authenticity rule: the on-camera person is never a product brand, "
    "label, ebook cover, package name, receipt title, sticker, or CTA asset. Do not put the creator name "
    "on any supermarket product or package. Use real supermarket products and "
    "ordinary retail packaging cues for food scenes, not creator-branded wellness products. Do not render "
    "prompt-control wording on packaging; instructions are not package labels."
)


class OmniStackError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env(find_env_file(args.env))
    config_path = Path(args.config).resolve()
    config = read_json(config_path)
    out_dir = Path(args.out).resolve() if args.out else default_run_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.mode == "dry-run":
            dry_run(config, config_path, out_dir)
        elif args.mode == "generate-library":
            generate_library(config, config_path, out_dir)
        elif args.mode == "build-variants":
            build_variants(config, config_path, out_dir)
        elif args.mode == "postprocess-video":
            postprocess_single_video(args, out_dir)
        elif args.mode == "postprocess-variants":
            postprocess_variants(out_dir, preset_name=args.postprocess_preset)
        elif args.mode == "all":
            generate_library(config, config_path, out_dir)
            build_variants(config, config_path, out_dir)
        else:
            raise OmniStackError(f"Unsupported mode: {args.mode}")
    except Exception as exc:
        print(f"Google Omni stack failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"Google Omni stack output: {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate reusable Gemini Omni Flash hook/main/CTA clips and variants.")
    parser.add_argument("--config", required=True, help="Path to Google Omni stack config JSON.")
    parser.add_argument(
        "--mode",
        choices=["dry-run", "generate-library", "build-variants", "postprocess-video", "postprocess-variants", "all"],
        default="dry-run",
    )
    parser.add_argument("--out", default="", help="Output run directory.")
    parser.add_argument("--env", default="", help="Optional env file. Defaults to .env.local if present.")
    parser.add_argument("--postprocess-input", default="", help="Input MP4 for --mode postprocess-video.")
    parser.add_argument("--postprocess-output", default="", help="Output MP4 for --mode postprocess-video.")
    parser.add_argument("--postprocess-preset", default="ugc_soft_60fps", help="Postprocess preset name.")
    return parser


def dry_run(config: dict[str, Any], config_path: Path, out_dir: Path) -> None:
    run_log = new_run_log("dry-run", config_path, out_dir)
    settings = google_settings(config, require_project=False)
    dry_requests: list[dict[str, Any]] = []
    for component in all_clip_specs(config):
        for segment_index, segment in enumerate(component_segments(component), start=1):
            prompt = build_clip_prompt(config, component, segment)
            request = {
                "clip_id": clip_id(component),
                "segment_id": segment_id(component, segment, segment_index),
                "role": clip_role(component),
                "duration_seconds": segment_duration(config, segment),
                "aspect_ratio": str(segment.get("aspect_ratio") or component.get("aspect_ratio") or defaults(config).get("aspect_ratio", "9:16")),
                "model": settings.model,
                "prompt": prompt,
                "reference_images": inherited_references(component, segment, "reference_images"),
                "reference_videos": inherited_references(component, segment, "reference_videos"),
            }
            dry_requests.append(request)
            write_json(
                out_dir / "dry_requests" / f"{request['role']}_{request['clip_id']}_{request['segment_id']}.json",
                request,
            )
    write_json(out_dir / "dry_run_manifest.json", {"clip_count": len(dry_requests), "clips": dry_requests})
    run_log["status"] = "succeeded"
    run_log["clip_count"] = len(dry_requests)
    write_json(out_dir / "run_log.json", run_log)


def generate_library(config: dict[str, Any], config_path: Path, out_dir: Path) -> None:
    run_log = new_run_log("generate-library", config_path, out_dir)
    settings = google_settings(config)
    components = all_clip_specs(config)
    manifest: list[dict[str, Any]] = []
    for index, component in enumerate(components, start=1):
        role = clip_role(component)
        cid = clip_id(component)
        clip_dir = out_dir / "library" / role / cid
        clip_dir.mkdir(parents=True, exist_ok=True)
        output_path = clip_dir / "clip.mp4"
        metadata_path = clip_dir / "clip.metadata.json"
        if output_path.exists() and output_path.stat().st_size > 0:
            metadata = read_json(metadata_path) if metadata_path.exists() else {"status": "reused"}
        else:
            metadata = generate_component(config, component, settings, clip_dir, output_path, out_dir)
        record = {
            "index": index,
            "role": role,
            "clip_id": cid,
            "title": component.get("title", cid),
            "duration_seconds": component_duration(config, component),
            "path": str(output_path),
            "metadata": metadata,
        }
        manifest.append(record)
        write_json(clip_dir / "clip_spec.json", component)
        write_json(out_dir / "library_manifest.json", manifest)
    run_log["status"] = "succeeded"
    run_log["clip_count"] = len(manifest)
    write_json(out_dir / "run_log.json", run_log)


def generate_component(
    config: dict[str, Any],
    component: dict[str, Any],
    settings: GoogleOmniSettings,
    clip_dir: Path,
    output_path: Path,
    out_dir: Path,
) -> dict[str, Any]:
    segment_outputs: list[Path] = []
    segment_metadata: list[dict[str, Any]] = []
    segments = component_segments(component)
    for segment_index, segment in enumerate(segments, start=1):
        sid = segment_id(component, segment, segment_index)
        segment_output = clip_dir / f"{sid}.mp4"
        prompt = build_clip_prompt(config, component, segment)
        write_json(clip_dir / f"{sid}.prompt.json", {"prompt": prompt})
        if segment_output.exists() and segment_output.stat().st_size > 0:
            metadata = read_json(segment_output.with_suffix(".metadata.json")) if segment_output.with_suffix(".metadata.json").exists() else {"status": "reused"}
        else:
            metadata = generate_clip(
                prompt=prompt,
                output_path=segment_output,
                settings=settings,
                duration_seconds=segment_duration(config, segment),
                aspect_ratio=str(segment.get("aspect_ratio") or component.get("aspect_ratio") or defaults(config).get("aspect_ratio", "9:16")),
                reference_images=inherited_references(component, segment, "reference_images"),
                reference_videos=inherited_references(component, segment, "reference_videos"),
                background=bool(provider(config).get("background", True)),
                log_path=out_dir / "google_omni_provider_log.jsonl",
            )
        segment_outputs.append(segment_output)
        segment_metadata.append({"segment_id": sid, "path": str(segment_output), "metadata": metadata})

    if len(segment_outputs) == 1:
        shutil.copyfile(segment_outputs[0], output_path)
    else:
        assemble_native_audio_clips(segment_outputs, output_path, log_path=clip_dir / "ffmpeg_component_log.txt")
    metadata = {
        "status": "succeeded",
        "provider": "google_omni_flash",
        "component_id": clip_id(component),
        "role": clip_role(component),
        "segments": segment_metadata,
        "duration_seconds": round(ffprobe_duration(output_path), 3),
        "output_path": str(output_path),
    }
    write_json(output_path.with_suffix(".metadata.json"), metadata)
    return metadata


def build_variants(config: dict[str, Any], config_path: Path, out_dir: Path) -> None:
    run_log = new_run_log("build-variants", config_path, out_dir)
    variant_config = dict(config.get("variants") or {})
    count = int(variant_config.get("count", 50))
    min_seconds = float(variant_config.get("min_total_seconds", 40))
    max_seconds = float(variant_config.get("max_total_seconds", 60))
    seed = int(variant_config.get("seed", 240701))
    random.seed(seed)

    library = load_or_resolve_library(config, out_dir)
    hooks = [item for item in library if item["role"] == "hooks"]
    mains = [item for item in library if item["role"] in {"mains", "meals"}]
    ctas = [item for item in library if item["role"] in {"ctas", "desserts", "closings"}]
    if not hooks or not mains or not ctas:
        raise OmniStackError("Variant assembly requires at least one hook, one main/meal, and one CTA/dessert clip.")

    combos = list(itertools.product(hooks, mains, ctas))
    random.shuffle(combos)
    selected: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for combo in combos:
        total = sum(float(item.get("duration_seconds", 0)) for item in combo)
        if min_seconds <= total <= max_seconds:
            selected.append(combo)
        if len(selected) >= count:
            break
    if not selected:
        raise OmniStackError(
            f"No hook/main/CTA combinations fit the requested {min_seconds}-{max_seconds}s duration window."
        )

    variant_manifest: list[dict[str, Any]] = []
    for variant_index, combo in enumerate(selected, start=1):
        variant_id = f"variant_{variant_index:03d}"
        variant_dir = out_dir / "variants" / variant_id
        variant_dir.mkdir(parents=True, exist_ok=True)
        clips = expand_variant_clips(combo, variant_config)
        for clip in clips:
            if not clip.exists() or clip.stat().st_size <= 0:
                raise OmniStackError(f"Missing clip for variant {variant_id}: {clip}")
        output_path = variant_dir / "final_video.mp4"
        assemble_native_audio_clips(clips, output_path, log_path=variant_dir / "ffmpeg_log.txt")
        record = {
            "variant_id": variant_id,
            "output_path": str(output_path),
            "duration_seconds": round(ffprobe_duration(output_path), 3),
            "components": [
                {
                    "role": item["role"],
                    "clip_id": item["clip_id"],
                    "title": item.get("title", item["clip_id"]),
                    "path": item["path"],
                }
                for item in combo
            ],
            "timeline_clips": [str(clip) for clip in clips],
            "stitch_leaf_segments": bool(variant_config.get("stitch_leaf_segments", False)),
        }
        write_json(variant_dir / "variant.json", record)
        variant_manifest.append(record)
    write_json(out_dir / "variant_manifest.json", variant_manifest)
    run_log["status"] = "succeeded"
    run_log["variant_count"] = len(variant_manifest)
    write_json(out_dir / "run_log.json", run_log)


def expand_variant_clips(combo: tuple[dict[str, Any], ...], variant_config: dict[str, Any]) -> list[Path]:
    if not bool(variant_config.get("stitch_leaf_segments", False)):
        return [Path(item["path"]).resolve() for item in combo]

    clips: list[Path] = []
    for item in combo:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        segments = metadata.get("segments") if isinstance(metadata.get("segments"), list) else []
        if not segments:
            clips.append(Path(item["path"]).resolve())
            continue
        for segment in segments:
            raw_segment_path = str(segment.get("path") or "").strip()
            if not raw_segment_path:
                raise OmniStackError(f"Missing segment path in metadata for {item.get('clip_id')}.")
            segment_path = Path(raw_segment_path).resolve()
            clips.append(segment_path)
    return clips


def postprocess_single_video(args: argparse.Namespace, out_dir: Path) -> None:
    if not args.postprocess_input:
        raise OmniStackError("--postprocess-input is required for --mode postprocess-video.")
    input_path = Path(args.postprocess_input).resolve()
    output_path = Path(args.postprocess_output).resolve() if args.postprocess_output else (
        out_dir / f"{input_path.stem}_{args.postprocess_preset}.mp4"
    )
    metadata = postprocess_video(
        input_path,
        output_path,
        preset_name=str(args.postprocess_preset),
        log_path=output_path.with_suffix(".ffmpeg_log.txt"),
    )
    write_json(output_path.with_suffix(".postprocess.json"), metadata)


def postprocess_variants(out_dir: Path, *, preset_name: str) -> None:
    variant_videos = sorted(out_dir.glob("variants/variant_*/final_video.mp4"))
    if not variant_videos:
        raise OmniStackError(f"No variant final videos found under {out_dir / 'variants'}.")
    records: list[dict[str, Any]] = []
    for video_path in variant_videos:
        output_path = video_path.with_name(f"final_video_{preset_name}.mp4")
        metadata = postprocess_video(
            video_path,
            output_path,
            preset_name=preset_name,
            log_path=output_path.with_suffix(".ffmpeg_log.txt"),
        )
        records.append(metadata)
    write_json(out_dir / f"postprocess_manifest_{preset_name}.json", records)


def assemble_native_audio_clips(clips: list[Path], output_path: Path, log_path: Path) -> None:
    concat_path = output_path.parent / "concat.txt"
    concat_path.write_text("\n".join(f"file '{path.as_posix()}'" for path in clips), encoding="utf-8")
    result = run_ffmpeg(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c",
            "copy",
            str(output_path),
        ],
        log_path=log_path,
        allow_failure=True,
    )
    if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        return
    run_ffmpeg(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-vf",
            "scale=1080:1920,fps=30,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(output_path),
        ],
        log_path=log_path,
    )


def run_ffmpeg(command: list[str], *, log_path: Path, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("Running FFmpeg: " + " ".join(command) + "\n")
    result = subprocess.run(command, capture_output=True, text=True, timeout=240, check=False)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(result.stdout)
        handle.write(result.stderr)
        handle.write(f"\nexit_code={result.returncode}\n")
    if result.returncode != 0 and not allow_failure:
        raise OmniStackError(f"FFmpeg failed. See {log_path}")
    return result


def load_or_resolve_library(config: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    manifest_path = out_dir / "library_manifest.json"
    if manifest_path.exists():
        return list(read_json(manifest_path))
    library: list[dict[str, Any]] = []
    for role in ("hooks", "mains", "meals", "ctas", "desserts", "closings"):
        for clip in list(config.get(role) or []):
            path = Path(str(clip.get("clip_path") or "")).expanduser()
            if not path.is_absolute():
                path = (WORKSPACE_ROOT / path).resolve()
            library.append(
                {
                    "role": canonical_role(role),
                    "clip_id": clip_id(clip),
                    "title": clip.get("title", clip_id(clip)),
                    "duration_seconds": int(clip.get("duration_seconds") or defaults(config).get("duration_seconds", 8)),
                    "path": str(path),
                }
            )
    return library


def all_clip_specs(config: dict[str, Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for role in ("hooks", "mains", "meals", "ctas", "desserts", "closings"):
        for item in list(config.get(role) or []):
            item = dict(item)
            item["_role"] = canonical_role(role)
            specs.append(item)
    if not specs:
        raise OmniStackError("Config does not contain hooks, mains/meals, or ctas/desserts.")
    return specs


def build_clip_prompt(config: dict[str, Any], component: dict[str, Any], segment: dict[str, Any] | None = None) -> str:
    segment = segment or {}
    component_prompt = str(component.get("prompt") or "").strip()
    segment_prompt = str(segment.get("prompt") or "").strip()
    if segment_prompt == component_prompt:
        segment_prompt = ""
    component_script = str(component.get("script") or "").strip()
    segment_script = str(segment.get("script") or "").strip()
    if segment_script == component_script:
        segment_script = ""
    prompt_parts = [
        str(config.get("master_prompt") or "").strip(),
        component_prompt,
        segment_prompt,
        component_script,
        segment_script,
    ]
    prompt = "\n\n".join(part for part in prompt_parts if part)
    if not prompt:
        raise OmniStackError(f"Clip {clip_id(component)} has no prompt/script.")
    return f"{prompt}\n\n{PRODUCT_BRANDING_RULE}"


def component_segments(component: dict[str, Any]) -> list[dict[str, Any]]:
    raw = component.get("segments")
    if isinstance(raw, list) and raw:
        return [dict(segment) for segment in raw]
    return [dict(component)]


def segment_id(component: dict[str, Any], segment: dict[str, Any], index: int) -> str:
    return slug(str(segment.get("id") or f"{clip_id(component)}_segment_{index:02d}"))


def segment_duration(config: dict[str, Any], segment: dict[str, Any]) -> int:
    duration = int(segment.get("duration_seconds") or defaults(config).get("duration_seconds", 8))
    if not (3 <= duration <= 10):
        raise OmniStackError(
            f"Google Omni segment duration must be 3-10 seconds, got {duration}. "
            "Use component.segments for longer meal/main blocks."
        )
    return duration


def component_duration(config: dict[str, Any], component: dict[str, Any]) -> int:
    return sum(segment_duration(config, segment) for segment in component_segments(component))


def inherited_references(component: dict[str, Any], segment: dict[str, Any], field: str) -> list[str]:
    if field in segment:
        return normalize_reference_list(segment.get(field))
    segment_refs = normalize_reference_list(segment.get(field))
    if segment_refs:
        return segment_refs
    return normalize_reference_list(component.get(field))


def google_settings(config: dict[str, Any], *, require_project: bool = True) -> GoogleOmniSettings:
    cfg = provider(config)
    project = os.environ.get(str(cfg.get("project_env") or "GOOGLE_CLOUD_PROJECT"), "").strip()
    project = str(cfg.get("project_id") or project).strip()
    if not project and require_project:
        raise OmniStackError("Set GOOGLE_CLOUD_PROJECT or provider.project_id before live generation.")
    if not project:
        project = "dry-run-project"
    return GoogleOmniSettings(
        project_id=project,
        model=str(cfg.get("model") or "gemini-omni-flash-preview"),
        location=str(cfg.get("location") or "global"),
        endpoint_base=str(cfg.get("endpoint_base") or "https://aiplatform.googleapis.com/v1beta1"),
        access_token_env=str(cfg.get("access_token_env") or "GOOGLE_OAUTH_ACCESS_TOKEN"),
        output_gcs_uri=str(cfg.get("output_gcs_uri") or ""),
        poll_seconds=int(cfg.get("poll_seconds") or 15),
        timeout_seconds=int(cfg.get("timeout_seconds") or 900),
        retries=int(cfg.get("retries") or 2),
    )


def provider(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("google_omni_flash") or config.get("provider") or {})


def defaults(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("defaults") or {})


def canonical_role(role: str) -> str:
    role = str(role).strip().lower()
    if role in {"meal", "meals", "main"}:
        return "mains"
    if role in {"cta", "ctas", "closing", "closings", "dessert", "desserts"}:
        return "ctas"
    return "hooks" if role == "hook" else role


def clip_role(clip: dict[str, Any]) -> str:
    return canonical_role(str(clip.get("_role") or clip.get("role") or "clips"))


def clip_id(clip: dict[str, Any]) -> str:
    raw = str(clip.get("id") or clip.get("clip_id") or clip.get("title") or "").strip()
    if not raw:
        raise OmniStackError(f"Clip missing id/title: {clip}")
    return slug(raw)


def slug(value: str) -> str:
    chars: list[str] = []
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
        elif chars and chars[-1] != "_":
            chars.append("_")
    return "".join(chars).strip("_") or "clip"


def normalize_reference_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise OmniStackError(f"Invalid reference list: {value}")


def new_run_log(mode: str, config_path: Path, out_dir: Path) -> dict[str, Any]:
    return {
        "pipeline": "google_omni_clip_stack",
        "mode": mode,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "config_path": str(config_path),
        "output_dir": str(out_dir),
        "status": "running",
    }


def default_run_dir() -> Path:
    return PIPELINE_DIR / "runs" / f"omni_stack_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def find_env_file(explicit: str) -> Path | None:
    if explicit:
        return Path(explicit).resolve()
    candidate = WORKSPACE_ROOT / ".env.local"
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


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise OmniStackError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    return float(result.stdout.strip())


if __name__ == "__main__":
    raise SystemExit(main())
