from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from ai_ugc_factory.adapters.video.ltx import LTX23AudioVideoProvider
from ai_ugc_factory.config import load_config
from ai_ugc_factory.contact_sheet import create_contact_sheet
from ai_ugc_factory.models import Scene, Timestamp
from ai_ugc_factory.utils import ensure_dir, write_json
from pipeline_v2_open_source import assemble_v2_video, split_scene_audio


def main() -> int:
    parser = argparse.ArgumentParser(description="Run direct LTX audio-to-video from existing images and audio.")
    parser.add_argument("--base-video-dir", type=Path, required=True)
    parser.add_argument("--source-images-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    provider = LTX23AudioVideoProvider()
    base_dir = args.base_video_dir
    source_images = args.source_images_dir
    out_dir = ensure_dir(args.out)

    audio_path = out_dir / "audio.mp3"
    shutil.copy2(base_dir / "audio.mp3", audio_path)
    for name in ("script.json", "timestamps.json", "prompts.json"):
        source = base_dir / name
        if source.exists():
            shutil.copy2(source, out_dir / name)

    timestamp_data = json.loads((base_dir / "timestamps.json").read_text(encoding="utf-8"))
    timestamps = [
        Timestamp(
            scene=int(item["scene"]),
            start=float(item["start"]),
            end=float(item["end"]),
            text=str(item["text"]),
        )
        for item in timestamp_data[:2]
    ]
    scenes: list[Scene] = []
    provider_log: list[dict] = []

    for item in timestamps:
        image_path = out_dir / f"scene_{item.scene:02d}.png"
        source_image = source_images / image_path.name
        if not source_image.exists():
            raise FileNotFoundError(f"Missing source image: {source_image}")
        shutil.copy2(source_image, image_path)

        scene_audio = out_dir / f"scene_{item.scene:02d}_audio.mp3"
        split_scene_audio(audio_path, scene_audio, item.start, item.duration, config)

        scene = Scene(
            scene=item.scene,
            role="direct_ltx_audio_test",
            narration=item.text,
            visual_description=(
                "Claire Natural speaking directly to the viewer in a hyper-realistic UGC selfie frame. "
                "Preserve the source image identity, object, kitchen setting, framing, lighting, and camera angle."
            ),
            prompt=item.text,
            duration=item.duration,
            start=item.start,
            end=item.end,
            image=image_path.name,
        )
        clip_path = out_dir / f"scene_{item.scene:02d}_ltx_audio.mp4"
        provider.generate_audio_video(image_path, scene_audio, clip_path, scene, config)
        scene.clip = clip_path.name
        scenes.append(scene)
        provider_log.append(provider.last_run_metadata)
        write_json(out_dir / "provider_log_direct_ltx_audio.json", provider_log)
        write_json(out_dir / "aligned_scenes.json", [scene.to_dict() for scene in scenes])

    write_json(out_dir / "scenes.json", [scene.to_dict() for scene in scenes])
    create_contact_sheet(out_dir, scenes, config)
    final_path = assemble_v2_video(out_dir, scenes, audio_path, timestamps, config)
    print(final_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
