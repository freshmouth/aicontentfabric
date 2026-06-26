from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from ai_ugc_factory.adapters.lipsync.musetalk import (
    MuseTalkLipSyncProvider,
    resolve_api_key as resolve_musetalk_api_key,
    resolve_endpoint_url as resolve_musetalk_endpoint_url,
)
from ai_ugc_factory.adapters.registry import IMAGE_PROVIDERS, VOICE_PROVIDERS
from ai_ugc_factory.adapters.transcriber import LocalEstimatorTranscriber
from ai_ugc_factory.adapters.transcriber_v2_whisper import (
    V2WhisperTranscriberProvider,
    resolve_api_key as resolve_whisper_api_key,
)
from ai_ugc_factory.adapters.video.ltx import (
    LTX23AudioVideoProvider,
    LTXVideoProvider,
    resolve_api_key as resolve_ltx_api_key,
    resolve_endpoint_url as resolve_ltx_endpoint_url,
    resolve_ltx_api_endpoint,
)
from ai_ugc_factory.config import FactoryConfig, load_config
from ai_ugc_factory.contact_sheet import create_contact_sheet
from ai_ugc_factory.models import ALLOWED_VIDEO_QUANTITIES, FactoryRequest, ProviderRun, Scene, Timestamp
from ai_ugc_factory.scene_planner import plan_scenes
from ai_ugc_factory.script_generator import generate_script
from ai_ugc_factory.subtitles import write_ass
from ai_ugc_factory.utils import ProviderUnavailable, ensure_dir, ffprobe_duration, write_json


T = TypeVar("T")


class V2OpenSourceFactory:
    def __init__(self, config: FactoryConfig, scene_limit: int | None = None) -> None:
        self.config = config
        self.scene_limit = scene_limit
        self.voice_providers = build_chain(config.providers.get("voice", []), VOICE_PROVIDERS, "voice")
        self.transcriber_provider = build_v2_transcriber_provider(config)
        self.image_providers = build_chain(config.providers.get("image", []), IMAGE_PROVIDERS, "image")
        self.video_provider = build_v2_video_provider(config)
        self.lipsync_provider = (
            None if selected_v2_video_provider(config) == "ltx_2_3_audio" else MuseTalkLipSyncProvider(config)
        )

    def run(self, request: FactoryRequest) -> list[Path]:
        request.validate()
        validate_v2_config(self.config)
        ensure_dir(request.output_dir)
        return [self._render_one(request, index) for index in range(1, request.videos + 1)]

    def _render_one(self, request: FactoryRequest, index: int) -> Path:
        video_dir = ensure_dir(request.output_dir / f"video_{index:02d}")
        provider_log_path = video_dir / "provider_log_v2.json"
        provider_log: list[dict[str, Any]] = []

        script = generate_script(request, index)
        write_json(video_dir / "script.json", script.to_dict())

        audio_path = video_dir / "audio.mp3"
        voice_run = try_chain(
            "voice",
            self.voice_providers,
            lambda provider: provider.generate_voice(script.voiceover, audio_path, self.config.character, self.config),
        )
        provider_log.append(voice_run.to_dict())
        write_json(provider_log_path, provider_log)

        all_scenes = plan_scenes(script, request, self.config.character)
        character_id = str(self.config.raw.get("character_id", "claire_natural"))
        for scene in all_scenes:
            scene.character_id = character_id

        if isinstance(self.transcriber_provider, V2WhisperTranscriberProvider):
            transcription = self.transcriber_provider.transcribe(audio_path, self.config)
            timestamps = self.transcriber_provider.align_scenes(
                transcription,
                audio_path,
                all_scenes,
                self.config,
            )
            metadata = self.transcriber_provider.last_run_metadata
        else:
            timestamps = self.transcriber_provider.generate_timestamps(audio_path, all_scenes, self.config)
            metadata = {
                "method": "local_duration_estimator",
                "reason": "whisper_not_selected",
            }
        provider_log.append(
            ProviderRun(
                kind="transcriber",
                selected=self.transcriber_provider.name,
                attempted=[self.transcriber_provider.name],
                metadata=metadata,
            ).to_dict()
        )
        write_json(provider_log_path, provider_log)

        timestamps_by_scene = {item.scene: item for item in timestamps}
        for scene in all_scenes:
            stamp = timestamps_by_scene[scene.scene]
            scene.start = stamp.start
            scene.end = stamp.end
            scene.duration = stamp.duration

        scenes = all_scenes if not self.scene_limit or self.scene_limit <= 0 else all_scenes[: self.scene_limit]
        selected_timestamps = [timestamps_by_scene[scene.scene] for scene in scenes]
        write_json(video_dir / "timestamps.json", [item.to_dict() for item in selected_timestamps])

        for scene in scenes:
            image_path = video_dir / f"scene_{scene.scene:02d}.png"
            image_run = try_chain(
                "image",
                self.image_providers,
                lambda provider, s=scene, p=image_path: provider.generate_image(
                    s, p, request, self.config.character, self.config
                ),
            )
            scene.image = image_path.name
            provider_log.append(image_run.to_dict())
            write_json(provider_log_path, provider_log)

            scene_audio_path = video_dir / f"scene_{scene.scene:02d}_audio.mp3"
            split_scene_audio(audio_path, scene_audio_path, scene.start, scene.duration, self.config)

            if isinstance(self.video_provider, LTX23AudioVideoProvider):
                ltx_audio_path = video_dir / f"scene_{scene.scene:02d}_ltx_audio.mp4"
                self.video_provider.generate_audio_video(image_path, scene_audio_path, ltx_audio_path, scene, self.config)
                provider_log.append(
                    ProviderRun(
                        kind="video",
                        selected=self.video_provider.name,
                        attempted=[self.video_provider.name],
                        metadata=self.video_provider.last_run_metadata,
                    ).to_dict()
                )
                scene.clip = ltx_audio_path.name
            else:
                ltx_path = video_dir / f"scene_{scene.scene:02d}_ltx.mp4"
                self.video_provider.generate_video(image_path, ltx_path, scene, self.config)
                provider_log.append(
                    ProviderRun(
                        kind="video",
                        selected=self.video_provider.name,
                        attempted=[self.video_provider.name],
                        metadata=self.video_provider.last_run_metadata,
                    ).to_dict()
                )
                write_json(provider_log_path, provider_log)

                if self.lipsync_provider is None:
                    raise RuntimeError("V2 ltx image-to-video mode requires a lipsync provider.")
                musetalk_path = video_dir / f"scene_{scene.scene:02d}_musetalk.mp4"
                self.lipsync_provider.generate_lipsync(ltx_path, scene_audio_path, musetalk_path)
                provider_log.append(
                    ProviderRun(
                        kind="lipsync",
                        selected=self.lipsync_provider.name,
                        attempted=[self.lipsync_provider.name],
                        metadata=self.lipsync_provider.last_run_metadata,
                    ).to_dict()
                )
                scene.clip = musetalk_path.name
            write_json(provider_log_path, provider_log)

        write_json(video_dir / "scenes.json", [scene.to_dict() for scene in scenes])
        write_json(video_dir / "aligned_scenes.json", [scene.to_dict() for scene in scenes])
        create_contact_sheet(video_dir, scenes, self.config)
        return assemble_v2_video(video_dir, scenes, audio_path, selected_timestamps, self.config)


def validate_v2_config(config: FactoryConfig) -> None:
    if str(config.raw.get("pipeline", "")).strip() != "v2_open_source":
        raise ValueError("V2 runner requires pipeline='v2_open_source'.")
    video_provider = selected_v2_video_provider(config)
    lipsync_provider = str(config.raw.get("lipsync_provider", "")).strip()
    if video_provider not in {"ltx", "ltx_2_3_audio"}:
        raise ValueError("V2 runner requires video_provider='ltx' or video_provider='ltx_2_3_audio'.")
    if video_provider == "ltx" and lipsync_provider != "musetalk":
        raise ValueError("V2 ltx image-to-video mode requires lipsync_provider='musetalk'.")
    if video_provider == "ltx_2_3_audio" and lipsync_provider not in {"none", ""}:
        raise ValueError("V2 ltx_2_3_audio mode must set lipsync_provider='none'.")
    transcribers = [str(value) for value in config.providers.get("transcriber", [])]
    if transcribers not in (["whisper"], ["local_estimator"]):
        raise ValueError("V2 runner requires providers.transcriber=['whisper'] or ['local_estimator'].")

    ltx_config = dict(config.raw.get(video_provider, {}) or config.raw.get("ltx", {}) or {})
    musetalk_config = dict(config.raw.get("musetalk", {}) or {})
    whisper_config = dict(config.raw.get("whisper", {}) or {})
    if selected_v2_transcriber(config) == "whisper":
        resolve_whisper_api_key(whisper_config)
    if video_provider == "ltx_2_3_audio":
        resolve_ltx_api_endpoint(ltx_config, "https://api.ltx.video/v2/audio-to-video")
    else:
        resolve_ltx_endpoint_url(ltx_config)
    resolve_ltx_api_key(ltx_config)
    if video_provider == "ltx":
        resolve_musetalk_endpoint_url(musetalk_config)
        resolve_musetalk_api_key(musetalk_config)


def selected_v2_video_provider(config: FactoryConfig) -> str:
    return str(config.raw.get("video_provider", "")).strip()


def selected_v2_transcriber(config: FactoryConfig) -> str:
    values = [str(value).strip() for value in config.providers.get("transcriber", [])]
    return values[0] if values else "whisper"


def build_v2_transcriber_provider(config: FactoryConfig) -> V2WhisperTranscriberProvider | LocalEstimatorTranscriber:
    selected = selected_v2_transcriber(config)
    if selected == "local_estimator":
        return LocalEstimatorTranscriber()
    return V2WhisperTranscriberProvider()


def build_v2_video_provider(config: FactoryConfig) -> LTXVideoProvider | LTX23AudioVideoProvider:
    selected = selected_v2_video_provider(config)
    if selected == "ltx_2_3_audio":
        return LTX23AudioVideoProvider()
    return LTXVideoProvider()


def split_scene_audio(
    source_audio_path: Path,
    output_path: Path,
    start: float,
    duration: float,
    config: FactoryConfig,
) -> None:
    run_ffmpeg(
        [
            config.ffmpeg_path,
            "-nostdin",
            "-y",
            "-ss",
            f"{max(0.0, float(start)):.3f}",
            "-i",
            str(source_audio_path),
            "-t",
            f"{max(0.0, float(duration)):.3f}",
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "128k",
            str(output_path),
        ],
        cwd=output_path.parent,
        timeout=120,
        log_path=output_path.parent / "ffmpeg_v2_log.txt",
    )


def assemble_v2_video(
    video_dir: Path,
    scenes: list[Scene],
    audio_path: Path,
    timestamps: list[Timestamp],
    config: FactoryConfig,
) -> Path:
    validate_v2_clips(video_dir, scenes, config)
    concat_path = video_dir / "concat_v2.txt"
    video_only_path = video_dir / "assembled_v2_video_only.mp4"
    reencoded_path = video_dir / "assembled_v2_reencoded.mp4"
    with_audio_path = video_dir / "assembled_v2_with_audio.mp4"
    subtitles_path = video_dir / "subtitles.ass"
    output_path = video_dir / "final_video_v2.mp4"
    log_path = video_dir / "ffmpeg_v2_log.txt"
    timeout = int(config.raw.get("ffmpeg_timeout_seconds") or 180)

    concat_path.write_text(
        "\n".join(f"file '{Path(scene.clip).name}'" for scene in scenes) + "\n",
        encoding="utf-8",
    )
    copy_result = run_ffmpeg(
        [
            config.ffmpeg_path,
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_path.name,
            "-c:v",
            "copy",
            "-an",
            video_only_path.name,
        ],
        cwd=video_dir,
        timeout=timeout,
        log_path=log_path,
        allow_failure=True,
    )

    assembly_input = video_only_path
    if copy_result.returncode != 0 or not video_only_path.exists() or video_only_path.stat().st_size <= 0:
        assembly_input = reencoded_path
        run_ffmpeg(
            [
                config.ffmpeg_path,
                "-nostdin",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_path.name,
                "-vf",
                f"scale={config.width}:{config.height},fps={config.fps},format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-an",
                reencoded_path.name,
            ],
            cwd=video_dir,
            timeout=timeout,
            log_path=log_path,
        )

    run_ffmpeg(
        [
            config.ffmpeg_path,
            "-nostdin",
            "-y",
            "-i",
            assembly_input.name,
            "-i",
            audio_path.name,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            with_audio_path.name,
        ],
        cwd=video_dir,
        timeout=timeout,
        log_path=log_path,
    )

    if bool(config.raw.get("subtitles_enabled", True)):
        write_ass(subtitles_path, timestamps, config.width, config.height)
        run_ffmpeg(
            [
                config.ffmpeg_path,
                "-nostdin",
                "-y",
                "-i",
                with_audio_path.name,
                "-vf",
                f"subtitles={subtitles_path.name}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "copy",
                output_path.name,
            ],
            cwd=video_dir,
            timeout=timeout,
            log_path=log_path,
        )
    else:
        shutil.copyfile(with_audio_path, output_path)
    return output_path


def validate_v2_clips(video_dir: Path, scenes: list[Scene], config: FactoryConfig) -> None:
    failures: list[str] = []
    for scene in scenes:
        clip_path = video_dir / (scene.clip or f"scene_{scene.scene:02d}_musetalk.mp4")
        if not clip_path.exists() or clip_path.stat().st_size <= 0:
            failures.append(f"scene_{scene.scene:02d}: missing or empty {clip_path.name}")
            continue
        try:
            if ffprobe_duration(clip_path, config.ffprobe_path) <= 0:
                failures.append(f"scene_{scene.scene:02d}: zero-duration {clip_path.name}")
        except Exception as exc:
            failures.append(f"scene_{scene.scene:02d}: invalid {clip_path.name} ({exc})")
    if failures:
        raise FileNotFoundError("V2 assembly cannot continue: " + "; ".join(failures))


def run_ffmpeg(
    args: list[str],
    cwd: Path,
    timeout: int,
    log_path: Path,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    command_text = subprocess.list2cmdline(args)
    print(f"Running FFmpeg: {command_text}")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {command_text}\n")
    started = time.monotonic()
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[timeout after {timeout}s]\n{exc.stderr or ''}\n")
        raise RuntimeError(f"V2 FFmpeg command timed out after {timeout} seconds. See {log_path}.") from exc

    elapsed = time.monotonic() - started
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[exit {result.returncode} after {elapsed:.2f}s]\n{result.stderr.strip()}\n\n")
    if result.returncode != 0 and not allow_failure:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"V2 FFmpeg command failed: {detail}. See {log_path}.")
    return result


def build_chain(names: list[str], registry: dict[str, type], kind: str) -> list[Any]:
    if not names:
        raise ValueError(f"No {kind} providers configured.")
    providers: list[Any] = []
    for name in names:
        provider_class = registry.get(name)
        if not provider_class:
            raise ValueError(f"Unknown {kind} provider '{name}'.")
        providers.append(provider_class())
    return providers


def try_chain(kind: str, providers: list[Any], action: Callable[[Any], None]) -> ProviderRun:
    _, run = try_chain_with_result(kind, providers, lambda provider: none(action(provider)))
    return run


def try_chain_with_result(kind: str, providers: list[Any], action: Callable[[Any], T]) -> tuple[T, ProviderRun]:
    attempted: list[str] = []
    errors: list[str] = []
    for provider in providers:
        attempted.append(provider.name)
        try:
            result = action(provider)
            metadata = getattr(provider, "last_run_metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            return result, ProviderRun(kind, provider.name, attempted, errors, metadata)
        except ProviderUnavailable as exc:
            errors.append(f"{provider.name}: {exc}")
        except Exception as exc:
            errors.append(f"{provider.name}: {exc}")
    raise RuntimeError(f"All {kind} providers failed: {' | '.join(errors)}")


def none(value: Any) -> None:
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Experimental open-source V2 pipeline")
    parser.add_argument("--quantity", type=int, choices=sorted(ALLOWED_VIDEO_QUANTITIES), default=1)
    parser.add_argument("--scenes", type=int, default=0, help="Limit scenes for development; 0 processes all scenes.")
    parser.add_argument("--topic", default="Natural Health")
    parser.add_argument("--product", default="Ebook")
    parser.add_argument("--cta", default="Comment BOOK")
    parser.add_argument("--out", default="project_v2_open_source")
    parser.add_argument("--config", default="config.v2_open_source.example.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(Path(args.config).resolve())
    request = FactoryRequest(
        topic=args.topic,
        product=args.product,
        cta=args.cta,
        videos=args.quantity,
        output_dir=Path(args.out).resolve(),
    )
    try:
        outputs = V2OpenSourceFactory(config, scene_limit=args.scenes).run(request)
    except Exception as exc:
        print(f"V2 open-source pipeline failed: {exc}", file=sys.stderr)
        return 1
    for output in outputs:
        print(f"Rendered V2 video: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
