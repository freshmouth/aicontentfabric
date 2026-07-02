from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from ai_ugc_factory.adapters.video.ltx import LTXOpenSourceAudioVideoProvider
from ai_ugc_factory.config import load_config
from ai_ugc_factory.contact_sheet import create_contact_sheet
from ai_ugc_factory.models import Scene, Timestamp
from ai_ugc_factory.utils import ensure_dir, write_json
from pipeline_v2_open_source import assemble_v2_video, split_scene_audio


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Smoke-test the persistent GPU LTX-2.3 audio-to-video worker using existing "
            "scene images and a finished ElevenLabs narration."
        )
    )
    parser.add_argument("--base-video-dir", type=Path, default=Path("claire_controversy_ltx_15s/video_01"))
    parser.add_argument("--source-images-dir", type=Path)
    parser.add_argument("--out", type=Path, default=Path("ltx_audio_persistent_smoke"))
    parser.add_argument("--config", type=Path, default=Path("config.v2_ltx_2_3_audio_persistent.example.json"))
    parser.add_argument("--scenes", type=int, default=1)
    args = parser.parse_args()

    if args.scenes < 1:
        raise ValueError("--scenes must be 1 or greater.")

    config = load_config(args.config)
    if str(config.raw.get("video_provider") or "") != "ltx_open_source_audio":
        raise ValueError("Persistent smoke config must use video_provider='ltx_open_source_audio'.")

    base_dir = args.base_video_dir
    source_images_dir = args.source_images_dir or base_dir
    if not (base_dir / "audio.mp3").exists():
        raise FileNotFoundError(f"Missing source narration: {base_dir / 'audio.mp3'}")
    if not (base_dir / "timestamps.json").exists():
        raise FileNotFoundError(f"Missing source timestamps: {base_dir / 'timestamps.json'}")

    out_dir = ensure_dir(args.out)
    audio_path = out_dir / "audio.mp3"
    shutil.copy2(base_dir / "audio.mp3", audio_path)
    copy_optional(base_dir, out_dir, ["script.json", "timestamps.json", "prompts.json", "subtitles.ass"])

    timestamp_data = json.loads((base_dir / "timestamps.json").read_text(encoding="utf-8"))
    timestamps = [
        Timestamp(
            scene=int(item["scene"]),
            start=float(item["start"]),
            end=float(item["end"]),
            text=str(item.get("text") or ""),
        )
        for item in timestamp_data[: args.scenes]
    ]
    if len(timestamps) < args.scenes:
        raise ValueError(f"Requested {args.scenes} scenes, but only found {len(timestamps)} timestamps.")

    provider = LTXOpenSourceAudioVideoProvider()
    scenes: list[Scene] = []
    provider_log: list[dict] = []

    for item in timestamps:
        scene_id = f"scene_{item.scene:02d}"
        image_path = out_dir / f"{scene_id}.png"
        source_image = source_images_dir / image_path.name
        if not source_image.exists():
            raise FileNotFoundError(f"Missing source image: {source_image}")
        shutil.copy2(source_image, image_path)

        scene_audio = out_dir / f"{scene_id}_audio.mp3"
        split_scene_audio(audio_path, scene_audio, item.start, item.duration, config)

        scene = Scene(
            scene=item.scene,
            role="persistent_ltx_audio_smoke",
            narration=item.text,
            visual_description=(
                "Claire Natural speaking directly to the viewer in a hyper-realistic UGC selfie frame. "
                "Use the input image as the exact visual source of truth. Preserve Claire's identity, "
                "object, kitchen setting, framing, lighting, camera angle, and direct eye contact."
            ),
            prompt=item.text,
            duration=item.duration,
            start=item.start,
            end=item.end,
            image=image_path.name,
        )
        clip_path = out_dir / f"{scene_id}_ltx_audio.mp4"
        provider.generate_audio_video(image_path, scene_audio, clip_path, scene, config)
        scene.clip = clip_path.name
        scenes.append(scene)
        provider_log.append(provider.last_run_metadata)
        write_json(out_dir / "provider_log_persistent_ltx_audio.json", provider_log)
        write_json(out_dir / "aligned_scenes.json", [scene.to_dict() for scene in scenes])

    write_json(out_dir / "scenes.json", [scene.to_dict() for scene in scenes])
    create_contact_sheet(out_dir, scenes, config)
    final_path = assemble_v2_video(out_dir, scenes, audio_path, timestamps, config)
    print(final_path)
    return 0


def copy_optional(source_dir: Path, target_dir: Path, names: list[str]) -> None:
    for name in names:
        source = source_dir / name
        if source.exists():
            shutil.copy2(source, target_dir / name)


if __name__ == "__main__":
    raise SystemExit(main())
