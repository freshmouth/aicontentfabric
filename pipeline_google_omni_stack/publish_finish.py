from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


class PublishFinishError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_video = Path(args.input).resolve()
    config_path = Path(args.config).resolve()
    out_dir = Path(args.out).resolve() if args.out else input_video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        metadata = finish_for_publish(
            input_video=input_video,
            config_path=config_path,
            out_dir=out_dir,
            hook_text=args.hook_text,
            silence_noise=args.silence_noise,
            silence_duration=float(args.silence_duration),
            keep_start=float(args.keep_start),
            keep_end=float(args.keep_end),
            caption_delay=float(args.caption_delay),
        )
    except Exception as exc:
        print(f"Publish finish failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(metadata, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remove silence and burn publish-ready hook caption/subtitles.")
    parser.add_argument("--input", required=True, help="Input MP4.")
    parser.add_argument("--config", required=True, help="Google Omni stack config used to create the video.")
    parser.add_argument("--out", default="", help="Output directory. Defaults to input video directory.")
    parser.add_argument(
        "--hook-text",
        default="Your salad dressing might be dessert oil",
        help="Top hook caption text.",
    )
    parser.add_argument("--silence-noise", default="-38dB")
    parser.add_argument("--silence-duration", default="0.35")
    parser.add_argument("--keep-start", default="0.06")
    parser.add_argument("--keep-end", default="0.08")
    parser.add_argument("--caption-delay", default="0.0", help="Seconds to delay regular subtitles after silence removal.")
    return parser


def finish_for_publish(
    *,
    input_video: Path,
    config_path: Path,
    out_dir: Path,
    hook_text: str,
    silence_noise: str,
    silence_duration: float,
    keep_start: float,
    keep_end: float,
    caption_delay: float,
) -> dict[str, Any]:
    if not input_video.exists() or input_video.stat().st_size <= 0:
        raise PublishFinishError(f"Missing input video: {input_video}")
    if not config_path.exists():
        raise PublishFinishError(f"Missing config: {config_path}")

    duration = ffprobe_duration(input_video)
    silence_log = out_dir / "silencedetect_log.txt"
    raw_silences = detect_silences(input_video, silence_noise, silence_duration, silence_log)
    cut_silences = build_cut_silences(raw_silences, keep_start=keep_start, keep_end=keep_end)
    keep_intervals = build_keep_intervals(duration, cut_silences)
    no_silence_video = out_dir / "final_video_no_silence.mp4"
    render_trimmed_video(input_video, no_silence_video, keep_intervals, out_dir / "ffmpeg_remove_silence_log.txt")
    trimmed_duration = ffprobe_duration(no_silence_video)

    config = read_json(config_path)
    transcription_audio = out_dir / "final_audio_for_transcription.mp3"
    extract_audio_for_transcription(
        no_silence_video,
        transcription_audio,
        out_dir / "ffmpeg_extract_transcription_audio_log.txt",
    )
    transcript = transcribe_final_audio(
        transcription_audio,
        language=str(config.get("language") or ""),
        api_key=os.getenv("OPENAI_API_KEY", "").strip(),
    )
    transcript_path = out_dir / "final_audio_transcript.json"
    write_json(transcript_path, transcript)
    hook_settings = {
        "enabled": True,
        "text": hook_text,
        "start": 0.0,
        "duration": 2.8,
        "font_size": 42,
        "max_chars_per_line": 27,
        "max_lines": 2,
        "top": 58,
        "box_padding_x": 10,
        "box_padding_y": 6,
        "box_radius": 10,
        "line_gap": 0,
        "line_height": 52,
        "fill_line_break_gaps": True,
        "box_seam_overlap": 8,
        "side_margin": 24,
        "char_width_factor": 0.5,
        "suppress_regular_captions": True,
    }
    caption_words = suppress_words_for_hook_overlay(list(transcript.get("words") or []), hook_settings)
    captions = build_captions_from_words(caption_words, trimmed_duration)
    captions = suppress_captions_for_hook_overlay(captions, hook_settings)
    subtitles_path = out_dir / "subtitles_publish.ass"
    write_ass_subtitles(captions, subtitles_path, width=720, height=1280)
    hook_events = add_hook_caption_to_ass(subtitles_path, hook_settings, width=720, height=1280)

    final_video = out_dir / "final_video_publish_ready.mp4"
    burn_subtitles(no_silence_video, subtitles_path, final_video, out_dir / "ffmpeg_burn_subtitles_log.txt")

    metadata = {
        "input_video": str(input_video),
        "config": str(config_path),
        "raw_duration_seconds": round(duration, 3),
        "trimmed_duration_seconds": round(trimmed_duration, 3),
        "removed_seconds": round(duration - trimmed_duration, 3),
        "silence_noise": silence_noise,
        "silence_duration": silence_duration,
        "caption_source": "final_audio_word_timestamps",
        "caption_delay": 0.0,
        "transcript": str(transcript_path),
        "transcription_audio": str(transcription_audio),
        "transcript_text": str(transcript.get("text") or ""),
        "raw_silences": raw_silences,
        "cut_silences": cut_silences,
        "keep_intervals": keep_intervals,
        "captions": len(captions),
        "hook_caption_events": hook_events,
        "no_silence_video": str(no_silence_video),
        "subtitles": str(subtitles_path),
        "final_video": str(final_video),
    }
    write_json(out_dir / "publish_finish_log.json", metadata)
    return metadata


def extract_audio_for_transcription(input_video: Path, output_audio: Path, log_path: Path) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(input_video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "96k",
        str(output_audio),
    ]
    run_command(command, log_path=log_path, timeout=120, allow_failure=False)


def transcribe_final_audio(video_path: Path, *, language: str, api_key: str) -> dict[str, Any]:
    """Transcribe the finished, silence-trimmed audio; never infer captions from scene metadata."""
    if not api_key:
        raise PublishFinishError("OPENAI_API_KEY is required to caption the final rendered audio.")
    try:
        import requests
    except ImportError as exc:
        raise PublishFinishError("requests is required for final-audio transcription.") from exc
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass

    language_code = language.strip().split("-", 1)[0].lower()
    data: list[tuple[str, str]] = [
        ("model", "whisper-1"),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"),
        ("temperature", "0"),
    ]
    if language_code:
        data.append(("language", language_code))
    mime_type = "audio/mpeg" if video_path.suffix.lower() == ".mp3" else "video/mp4"
    with video_path.open("rb") as audio_file:
        response = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data=data,
            files={"file": (video_path.name, audio_file, mime_type)},
            timeout=180,
        )
    if response.status_code >= 400:
        raise PublishFinishError(
            f"Final-audio transcription failed: HTTP {response.status_code}: {response.text[:800]}"
        )
    payload = response.json()
    words: list[dict[str, Any]] = []
    for item in list(payload.get("words") or []):
        token = str(item.get("word") or "").strip()
        if not token:
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PublishFinishError("Final-audio transcription returned invalid word timestamps.") from exc
        words.append({"word": token, "start": start, "end": max(start + 0.04, end)})
    if not words:
        raise PublishFinishError("Final-audio transcription returned no word timestamps; captions were not burned.")
    return {
        "model": "whisper-1",
        "language": payload.get("language") or language_code,
        "duration": payload.get("duration"),
        "text": str(payload.get("text") or "").strip(),
        "words": words,
    }


def build_captions_from_words(words: list[dict[str, Any]], final_duration: float) -> list[dict[str, Any]]:
    """Group real word timestamps without ever showing a caption before its first spoken word."""
    captions: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []
    for word in words:
        if float(word.get("start") or 0.0) >= final_duration:
            break
        if chunk and float(word.get("start") or 0.0) - float(chunk[-1].get("end") or 0.0) >= 0.20:
            captions.append(caption_from_word_chunk(chunk, final_duration))
            chunk = []
        chunk.append(word)
        text = " ".join(str(item.get("word") or "").strip() for item in chunk).strip()
        token = str(word.get("word") or "").strip()
        if len(chunk) >= 4 or len(text) >= 30 or token.endswith((".", "?", "!")):
            captions.append(caption_from_word_chunk(chunk, final_duration))
            chunk = []
    if chunk:
        captions.append(caption_from_word_chunk(chunk, final_duration))
    captions = [item for item in captions if item["end"] - item["start"] >= 0.12]
    for index in range(len(captions) - 1):
        next_start = float(captions[index + 1]["start"])
        if float(captions[index]["end"]) > next_start:
            captions[index]["end"] = round(max(float(captions[index]["start"]) + 0.12, next_start), 3)
    return captions


def caption_from_word_chunk(words: list[dict[str, Any]], final_duration: float) -> dict[str, Any]:
    start = max(0.0, min(final_duration, float(words[0]["start"])))
    end = max(start + 0.12, min(final_duration, float(words[-1]["end"]) + 0.04))
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "text": " ".join(str(item["word"]).strip() for item in words).strip(),
    }


def detect_silences(video_path: Path, noise: str, silence_duration: float, log_path: Path) -> list[dict[str, float]]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(video_path),
        "-af",
        f"silencedetect=noise={noise}:d={silence_duration}",
        "-f",
        "null",
        "-",
    ]
    result = run_command(command, log_path=log_path, timeout=120, allow_failure=False)
    return parse_silencedetect(result.stderr)


def parse_silencedetect(text: str) -> list[dict[str, float]]:
    starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    end_matches = re.findall(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", text)
    events: list[dict[str, float]] = []
    for index, (end_value, duration_value) in enumerate(end_matches):
        if index >= len(starts):
            break
        events.append(
            {
                "start": round(starts[index], 6),
                "end": round(float(end_value), 6),
                "duration": round(float(duration_value), 6),
            }
        )
    return events


def build_cut_silences(raw: list[dict[str, float]], *, keep_start: float, keep_end: float) -> list[dict[str, float]]:
    cuts: list[dict[str, float]] = []
    for item in raw:
        start = float(item["start"]) + keep_start
        end = float(item["end"]) - keep_end
        if end - start >= 0.08:
            cuts.append({"start": round(start, 6), "end": round(end, 6), "duration": round(end - start, 6)})
    return merge_intervals(cuts)


def merge_intervals(intervals: list[dict[str, float]]) -> list[dict[str, float]]:
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda item: item["start"])
    merged = [dict(sorted_intervals[0])]
    for item in sorted_intervals[1:]:
        last = merged[-1]
        if item["start"] <= last["end"] + 0.04:
            last["end"] = max(last["end"], item["end"])
            last["duration"] = round(last["end"] - last["start"], 6)
        else:
            merged.append(dict(item))
    return merged


def build_keep_intervals(duration: float, cuts: list[dict[str, float]]) -> list[dict[str, float]]:
    intervals: list[dict[str, float]] = []
    cursor = 0.0
    for cut in cuts:
        start = max(0.0, min(duration, float(cut["start"])))
        end = max(0.0, min(duration, float(cut["end"])))
        if start > cursor + 0.02:
            intervals.append({"start": round(cursor, 6), "end": round(start, 6)})
        cursor = max(cursor, end)
    if cursor < duration - 0.02:
        intervals.append({"start": round(cursor, 6), "end": round(duration, 6)})
    return intervals


def render_trimmed_video(input_video: Path, output_video: Path, keep_intervals: list[dict[str, float]], log_path: Path) -> None:
    if not keep_intervals:
        raise PublishFinishError("No keep intervals generated.")
    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, interval in enumerate(keep_intervals):
        start = interval["start"]
        end = interval["end"]
        filters.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]")
        filters.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]")
        concat_inputs.append(f"[v{index}][a{index}]")
    filters.append("".join(concat_inputs) + f"concat=n={len(keep_intervals)}:v=1:a=1[outv][outa]")
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(input_video),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output_video),
    ]
    run_command(command, log_path=log_path, timeout=180, allow_failure=False)


def source_scene_timeline(config: dict[str, Any], variant_path: Path) -> list[dict[str, Any]]:
    variant = read_json(variant_path)
    timeline_clips = [Path(path) for path in variant.get("timeline_clips", [])]
    prompts_by_id: dict[str, str] = {}
    scene_order: list[tuple[str, str, str]] = []
    for hook in config.get("hooks", []):
        scene_order.append((str(hook.get("id")), str(hook.get("prompt", "")), scene_dialogue_text(hook)))
    for main in config.get("meals", []) + config.get("mains", []):
        for segment in main.get("segments", []):
            scene_order.append((str(segment.get("id")), str(segment.get("prompt", "")), scene_dialogue_text(segment)))
    for cta in config.get("ctas", []):
        scene_order.append((str(cta.get("id")), str(cta.get("prompt", "")), scene_dialogue_text(cta)))
    for scene_id, prompt, explicit_dialogue in scene_order:
        dialogue = explicit_dialogue or extract_native_dialogue(prompt)
        if dialogue:
            prompts_by_id[scene_id] = dialogue

    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    for clip in timeline_clips:
        duration = ffprobe_duration(clip)
        scene_id = clip.stem
        text = prompts_by_id.get(scene_id, scene_id.replace("_", " "))
        timeline.append({"id": scene_id, "start": cursor, "end": cursor + duration, "text": text})
        cursor += duration
    return timeline


def scene_dialogue_text(scene: dict[str, Any]) -> str:
    for key in ("dialogue", "script", "native_dialogue", "spoken_line"):
        text = str(scene.get(key) or "").strip()
        if text:
            return text.strip().strip('"')
    return ""


def extract_native_dialogue(prompt: str) -> str:
    match = re.search(r"Native dialogue:\s*(.+)$", prompt, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    text = match.group(1).strip()
    text = re.split(r"\n\n|Product authenticity rule:", text, maxsplit=1)[0].strip()
    return text.strip().strip('"')


def build_captions(
    scene_timeline: list[dict[str, Any]],
    keep_intervals: list[dict[str, float]],
    final_duration: float,
    *,
    caption_delay: float = 0.0,
) -> list[dict[str, Any]]:
    captions: list[dict[str, Any]] = []
    for scene in scene_timeline:
        scene_start = map_time(float(scene["start"]), keep_intervals)
        scene_end = map_time(float(scene["end"]), keep_intervals)
        scene_start = max(0.0, min(final_duration, scene_start))
        scene_end = max(scene_start + 0.2, min(final_duration, scene_end))
        chunks = split_caption_text(str(scene["text"]), max_words=3)
        if not chunks:
            continue
        weights = [max(1, len(chunk.split())) for chunk in chunks]
        total = sum(weights)
        cursor = scene_start
        available = max(0.35, scene_end - scene_start)
        for index, (chunk, weight) in enumerate(zip(chunks, weights, strict=True)):
            if index == len(chunks) - 1:
                end = scene_end
            else:
                end = cursor + available * (weight / total)
            caption_start = min(max(0.0, cursor + caption_delay), max(0.0, end - 0.12))
            if end - caption_start >= 0.18:
                captions.append({"start": round(caption_start, 3), "end": round(end, 3), "text": chunk})
            cursor = end
    return captions


def map_time(old_time: float, keep_intervals: list[dict[str, float]]) -> float:
    new_time = 0.0
    for interval in keep_intervals:
        start = float(interval["start"])
        end = float(interval["end"])
        if old_time <= start:
            break
        new_time += max(0.0, min(old_time, end) - start)
        if old_time <= end:
            break
    return new_time


def split_caption_text(text: str, max_words: int) -> list[str]:
    words = text.replace("—", " ").replace("–", " ").split()
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        current.append(word)
        if len(current) >= max_words or word.endswith((".", "?", "!")):
            chunks.append(" ".join(current).strip())
            current = []
    if current:
        chunks.append(" ".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def suppress_captions_for_hook_overlay(captions: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(settings.get("enabled", False)) or not bool(settings.get("suppress_regular_captions", True)):
        return captions
    start = float(settings.get("start", 0.0))
    end = start + float(settings.get("duration", 3.2))
    visible: list[dict[str, Any]] = []
    for caption in captions:
        caption_start = float(caption["start"])
        caption_end = float(caption["end"])
        if caption_end <= start or caption_start >= end:
            visible.append(caption)
            continue
        if caption_end > end + 0.12:
            trimmed = dict(caption)
            trimmed["start"] = round(end, 3)
            visible.append(trimmed)
    return visible


def suppress_words_for_hook_overlay(words: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Remove only words already represented by the opening hook, before caption grouping."""
    if not bool(settings.get("enabled", False)) or not bool(settings.get("suppress_regular_captions", True)):
        return words
    end = float(settings.get("start", 0.0)) + float(settings.get("duration", 3.2))
    return [word for word in words if float(word.get("start") or 0.0) >= end]


def write_ass_subtitles(captions: list[dict[str, Any]], output_path: Path, *, width: int, height: int) -> None:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,DejaVu Sans,40,&H00FFFFFF,&H000000FF,&HCC000000,&HAA000000,-1,0,0,0,100,100,0,0,1,4,0,2,86,86,146,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for caption in captions:
        text = escape_ass_text(str(caption["text"]))
        lines.append(
            f"Dialogue: 2,{ass_timestamp(float(caption['start']))},{ass_timestamp(float(caption['end']))},Default,,0,0,0,,{text}"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_hook_caption_to_ass(subtitles_path: Path, settings: dict[str, Any], width: int, height: int) -> int:
    if not bool(settings.get("enabled", False)):
        return 0
    text = str(settings.get("text") or "").strip()
    if not text:
        return 0
    lines = wrap_hook_caption_text(text, int(settings.get("max_chars_per_line", 24)), int(settings.get("max_lines", 3)))
    if not lines:
        return 0
    content = subtitles_path.read_text(encoding="utf-8")
    style_name = "HookCaption"
    font_size = int(settings.get("font_size", 74))
    style_line = (
        f"Style: {style_name},DejaVu Sans,{font_size},&H00000000,&H000000FF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,1,0,0,5,60,60,80,1"
    )
    content = content.replace("Style: Default,", style_line + "\nStyle: Default,", 1)
    start = float(settings.get("start", 0.0))
    end = start + float(settings.get("duration", 3.25))
    x = width // 2
    top = int(settings.get("top", 72))
    padding_x = int(settings.get("box_padding_x", 22))
    padding_y = int(settings.get("box_padding_y", 10))
    box_height = int(settings.get("box_height", font_size + padding_y * 2))
    line_height = int(settings.get("line_height", box_height))
    seam_overlap = int(settings.get("box_seam_overlap", 10)) if len(lines) > 1 else 0
    radius = int(settings.get("box_radius", 14))
    side_margin = int(settings.get("side_margin", 20))
    events: list[str] = []
    for index, line in enumerate(lines):
        box_width = estimate_hook_caption_box_width(line, font_size, padding_x, width, side_margin, settings)
        box_top = top + index * line_height
        draw_height = box_height + (seam_overlap if index < len(lines) - 1 else 0)
        box_left = max(side_margin, min(width - side_margin - box_width, int(round(x - box_width / 2))))
        box_path = rounded_rectangle_ass_path(box_width, draw_height, radius)
        events.append(
            f"Dialogue: 4,{ass_timestamp(start)},{ass_timestamp(end)},Default,,0,0,0,,"
            f"{{\\an7\\pos({box_left},{box_top})\\bord0\\shad0\\1c&HFFFFFF&\\1a&H00&\\p1}}{box_path}{{\\p0}}"
        )
        events.append(
            f"Dialogue: 5,{ass_timestamp(start)},{ass_timestamp(end)},{style_name},,0,0,0,,"
            f"{{\\an5\\pos({x},{box_top + round(box_height / 2)})}}{escape_ass_text(line)}"
        )
    marker = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    content = content.replace(marker, marker + "\n".join(events) + "\n", 1)
    subtitles_path.write_text(content, encoding="utf-8")
    return len(events)


def estimate_hook_caption_box_width(line: str, font_size: int, padding_x: int, video_width: int, side_margin: int, settings: dict[str, Any]) -> int:
    factor = float(settings.get("char_width_factor", 0.43))
    estimated = int(round(len(line) * font_size * factor + padding_x * 2))
    return max(260, min(video_width - side_margin * 2, estimated))


def rounded_rectangle_ass_path(width: int, height: int, radius: int) -> str:
    width = max(2, int(width))
    height = max(2, int(height))
    radius = max(0, min(int(radius), width // 2, height // 2))
    if radius <= 0:
        return f"m 0 0 l {width} 0 l {width} {height} l 0 {height}"
    kappa = max(1, int(round(radius * 0.55228475)))
    return " ".join(
        [
            f"m {radius} 0",
            f"l {width - radius} 0",
            f"b {width - radius + kappa} 0 {width} {radius - kappa} {width} {radius}",
            f"l {width} {height - radius}",
            f"b {width} {height - radius + kappa} {width - radius + kappa} {height} {width - radius} {height}",
            f"l {radius} {height}",
            f"b {radius - kappa} {height} 0 {height - radius + kappa} 0 {height - radius}",
            f"l 0 {radius}",
            f"b 0 {radius - kappa} {radius - kappa} 0 {radius} 0",
        ]
    )


def wrap_hook_caption_text(text: str, max_chars_per_line: int, max_lines: int) -> list[str]:
    explicit_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(explicit_lines) > 1:
        return explicit_lines[:max_lines]
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and len(candidate) > max_chars_per_line and len(lines) < max_lines - 1:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [" ".join(lines[max_lines - 1 :])]
    return lines


def burn_subtitles(input_video: Path, subtitles_path: Path, output_video: Path, log_path: Path) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(input_video),
        "-vf",
        f"subtitles={subtitles_path.name}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output_video),
    ]
    run_command(command, log_path=log_path, timeout=180, allow_failure=False, cwd=subtitles_path.parent)


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise PublishFinishError(f"ffprobe failed for {path}: {result.stderr}")
    return float(result.stdout.strip())


def run_command(
    command: list[str],
    *,
    log_path: Path,
    timeout: int,
    allow_failure: bool,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd, stdin=subprocess.DEVNULL)
    log_path.write_text(
        "Running: " + " ".join(command) + "\n\nSTDOUT:\n" + result.stdout + "\nSTDERR:\n" + result.stderr,
        encoding="utf-8",
    )
    if result.returncode != 0 and not allow_failure:
        raise PublishFinishError(f"Command failed. See {log_path}")
    return result


def ass_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds * 100)))
    cs = total % 100
    total //= 100
    sec = total % 60
    total //= 60
    minute = total % 60
    hour = total // 60
    return f"{hour}:{minute:02d}:{sec:02d}.{cs:02d}"


def escape_ass_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ").strip()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
