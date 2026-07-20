from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from Mix.v3.image_prompting import build_image_prompt
    from Mix.v3.image_qa import build_fixed_prompt, build_qa_prompt
    from Mix.v3.subject import SubjectDescriptor
else:
    from .image_prompting import build_image_prompt
    from .image_qa import build_fixed_prompt, build_qa_prompt
    from .subject import SubjectDescriptor


ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mix v3 composed execution with dynamic subject support.")
    parser.add_argument("--config", required=True, help="V3 Mix config JSON.")
    parser.add_argument("--out", default="", help="Output directory. Defaults to Mix/runs/v3_YYYYMMDD_HHMMSS.")
    parser.add_argument("--generate-first-image", action="store_true", help="Generate only one first-frame image from the V3 image prompt.")
    parser.add_argument("--first-image-index", type=int, default=1, help="1-based scene index for --generate-first-image.")
    parser.add_argument("--execute-video", action="store_true", help="After v3 composition, run the existing Omni stack.")
    parser.add_argument("--env-ssl-no-verify", action="store_true", help="Set GOOGLE_OMNI_SSL_NO_VERIFY=true for Omni execution.")
    args = parser.parse_args(argv)

    load_env(ROOT / ".env.local")
    config_path = Path(args.config).resolve()
    config = read_json(config_path)
    out_dir = Path(args.out).resolve() if args.out else default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    source_config = resolve_path(config_path.parent, str(config["source_config"]))
    source = read_json(source_config)
    project = {
        "subject_label": config.get("subject_label"),
        "subject_placement_hint": config.get("subject_placement_hint"),
    }
    scenes = collect_scenes(source)
    records: list[dict[str, Any]] = []
    for index, scene in enumerate(scenes, start=1):
        subject = SubjectDescriptor.from_mapping(project, scene)
        image_prompt = build_image_prompt(scene, project=project)
        qa_prompt = build_qa_prompt(image_prompt, project=project, scene=scene)
        simulated_fail = dict(config.get("simulated_qa_failure") or {})
        fixed_prompt = build_fixed_prompt(image_prompt, simulated_fail, project=project, scene=scene) if simulated_fail else image_prompt
        record = {
            "index": index,
            "role": scene.get("role", ""),
            "component_id": scene.get("component_id", ""),
            "scene_id": scene.get("id", f"scene_{index:02d}"),
            "duration_seconds": scene.get("duration_seconds"),
            "subject_label": subject.label,
            "subject_placement_hint": subject.placement_hint,
            "image_prompt": image_prompt,
            "qa_prompt": qa_prompt,
            "fixed_prompt_preview": fixed_prompt,
        }
        records.append(record)
        write_json(out_dir / "image_prompts" / f"{index:02d}_{record['scene_id']}.json", record)

    manifest = {
        "schema_version": 3,
        "module": "mix_v3_dynamic_subject",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_config": str(source_config),
        "subject_label": SubjectDescriptor.from_mapping(project).label,
        "subject_placement_hint": SubjectDescriptor.from_mapping(project).placement_hint,
        "stages": [
            "comprehension",
            "creative_brain_scene_plan",
            "image_prompt_generation",
            "image_qa_gate",
            "video_generation_existing_omni_stack",
            "video_qa_gate_placeholder",
            "assembly_existing_hard_cut",
            "captions_optional",
            "publish_optional",
        ],
        "scene_count": len(records),
        "scenes": records,
        "generated_first_image": "",
        "execute_video": bool(args.execute_video),
    }

    if args.generate_first_image:
        first_image = generate_first_image(records, args.first_image_index, out_dir)
        manifest["generated_first_image"] = str(first_image)

    write_json(out_dir / "v3_execution_manifest.json", manifest)

    if args.execute_video:
        env = None
        if args.env_ssl_no_verify:
            import os

            env = os.environ.copy()
            env["GOOGLE_OMNI_SSL_NO_VERIFY"] = "true"
        omni_out = out_dir / "omni"
        command = [
            sys.executable,
            str(ROOT / "pipeline_google_omni_stack" / "omni_stack_runner.py"),
            "--config",
            str(source_config),
            "--mode",
            "all",
            "--out",
            str(omni_out),
        ]
        result = subprocess.run(command, text=True, capture_output=True, timeout=7200, check=False, env=env)
        (out_dir / "omni_stdout.txt").write_text(result.stdout, encoding="utf-8")
        (out_dir / "omni_stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise SystemExit(f"Omni execution failed. See {out_dir / 'omni_stderr.txt'}")

    print(json.dumps({"status": "ok", "manifest": str(out_dir / "v3_execution_manifest.json")}, indent=2))
    return 0


def generate_first_image(records: list[dict[str, Any]], index: int, out_dir: Path) -> Path:
    if not 1 <= index <= len(records):
        raise SystemExit(f"--first-image-index must be between 1 and {len(records)}.")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required for --generate-first-image.")
    from daily_factory.first_frames import generate_openai_first_frame

    record = records[index - 1]
    output_path = out_dir / "first_images" / f"{index:02d}_{record['scene_id']}.png"
    prompt_path = output_path.with_suffix(".prompt.txt")
    prompt = str(record["image_prompt"])
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    generate_openai_first_frame(
        prompt=prompt,
        output_path=output_path,
        reference_images=[],
        api_key=api_key,
        settings={
            "model": "gpt-image-2",
            "size": "720x1280",
            "quality": "medium",
            "timeout": 240,
            "ffmpeg_path": "ffmpeg",
        },
    )
    return output_path


def collect_scenes(config: dict[str, Any]) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    for role in ("hooks", "mains", "meals", "ctas", "desserts", "closings"):
        for component in list(config.get(role) or []):
            component_id = str(component.get("id") or component.get("title") or role)
            segments = component.get("segments")
            if isinstance(segments, list) and segments:
                for segment in segments:
                    item = dict(segment)
                    item["role"] = role
                    item["component_id"] = component_id
                    if "prompt" not in item and component.get("prompt"):
                        item["prompt"] = component.get("prompt")
                    scenes.append(item)
            else:
                item = dict(component)
                item["role"] = role
                item["component_id"] = component_id
                scenes.append(item)
    return scenes


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def default_out_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "Mix" / "runs" / f"v3_{stamp}"


if __name__ == "__main__":
    raise SystemExit(main())
