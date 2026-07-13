from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PostprocessError(RuntimeError):
    pass


@dataclass(frozen=True)
class FilterPreset:
    name: str = "ugc_soft_60fps"
    exposure: float = -0.04
    contrast: float = 0.92
    saturation: float = 1.0
    temperature: int = 5200
    tint: float = 0.0
    highlights: float = 0.0
    shadows: float = 0.0
    fade: float = 0.0
    sharpen: float = 0.12
    vignette_angle: str = "PI/5"
    vignette_enabled: bool = True
    fade_gamma: float = 1.0
    target_fps: int = 60
    optical_flow: bool = True
    scene_change_threshold: float = 8.0


PRESETS: dict[str, FilterPreset] = {
    "ugc_soft_60fps": FilterPreset(),
    "ugc_soft_30fps": FilterPreset(name="ugc_soft_30fps", optical_flow=False, target_fps=30),
    "ugc_crisp_60fps": FilterPreset(name="ugc_crisp_60fps", contrast=0.95, sharpen=0.22),
    "capcut_ugc_adjustments_30fps": FilterPreset(
        name="capcut_ugc_adjustments_30fps",
        exposure=-0.015,
        contrast=1.055,
        saturation=0.98,
        temperature=6500,
        tint=0.004,
        highlights=-0.35,
        shadows=0.18,
        fade=0.025,
        sharpen=0.025,
        vignette_enabled=False,
        optical_flow=False,
        target_fps=30,
    ),
    "capcut_ugc_adjustments_60fps": FilterPreset(
        name="capcut_ugc_adjustments_60fps",
        exposure=-0.015,
        contrast=1.055,
        saturation=0.98,
        temperature=6500,
        tint=0.004,
        highlights=-0.35,
        shadows=0.18,
        fade=0.025,
        sharpen=0.025,
        vignette_enabled=False,
        optical_flow=True,
        target_fps=60,
    ),
}


def build_filter_string(preset: FilterPreset) -> str:
    curves_filter = build_adjustment_curves(preset)
    color_balance = build_color_balance(preset)
    filters = [
        # Exposure, contrast, saturation.
        f"eq=contrast={preset.contrast}:brightness={preset.exposure}:saturation={preset.saturation}:gamma={preset.fade_gamma}",
        # Fade, shadows, and highlights.
        curves_filter,
        # Warm/cool color temperature.
        f"colortemperature=temperature={preset.temperature}",
        color_balance,
        # Sharpen/clarity. Keep subtle for UGC faces to avoid crunchy AI artifacts.
        f"unsharp=5:5:{preset.sharpen}:3:3:0.0",
    ]
    if preset.vignette_enabled:
        filters.append(f"vignette={preset.vignette_angle}")
    if preset.optical_flow:
        filters.append(
            "minterpolate="
            f"fps={preset.target_fps}:"
            "mi_mode=mci:"
            "mc_mode=aobmc:"
            "me_mode=bidir:"
            "vsbmc=1:"
            f"scd=fdiff:scd_threshold={preset.scene_change_threshold}"
        )
    else:
        filters.append(f"fps={preset.target_fps}")
    filters.append("format=yuv420p")
    return ",".join(filters)


def build_adjustment_curves(preset: FilterPreset) -> str:
    """Approximate editor sliders with a stable luma curve.

    Fade lifts black point, shadows lift the lower-mid curve, and negative highlights
    compress the top end without making skin look artificially HDR.
    """
    black_out = clamp(preset.fade * 0.45, 0.0, 0.05)
    shadow_out = clamp(0.22 + preset.fade * 0.35 + preset.shadows * 0.16, 0.18, 0.30)
    mid_out = clamp(0.50 + preset.shadows * 0.025 + preset.highlights * 0.012, 0.47, 0.53)
    highlight_out = clamp(0.82 + preset.highlights * 0.085, 0.76, 0.86)
    white_out = clamp(1.0 + preset.highlights * 0.045, 0.96, 1.0)
    points = (
        f"0/{black_out:.4f} "
        f"0.22/{shadow_out:.4f} "
        f"0.50/{mid_out:.4f} "
        f"0.82/{highlight_out:.4f} "
        f"1/{white_out:.4f}"
    )
    return f"curves=all='{points}'"


def build_color_balance(preset: FilterPreset) -> str:
    # Temperature -3 from the editor is represented by a slight cool lift. Tint +2 is a
    # tiny magenta bias. These are intentionally restrained to avoid fake-looking skin.
    neutral = 6500
    cool = clamp((preset.temperature - neutral) / 20000.0, -0.08, 0.08)
    tint = clamp(preset.tint, -0.08, 0.08)
    rs = -cool
    bs = cool
    rm = tint * 0.45
    gm = -tint * 0.35
    bm = tint * 0.20
    return f"colorbalance=rs={rs:.4f}:bs={bs:.4f}:rm={rm:.4f}:gm={gm:.4f}:bm={bm:.4f}"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def postprocess_video(
    input_path: Path,
    output_path: Path,
    *,
    preset_name: str = "ugc_soft_60fps",
    log_path: Path | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists() or input_path.stat().st_size <= 0:
        raise PostprocessError(f"Input video does not exist: {input_path}")
    preset = PRESETS.get(preset_name)
    if not preset:
        raise PostprocessError(f"Unknown postprocess preset '{preset_name}'. Options: {', '.join(sorted(PRESETS))}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_string = build_filter_string(preset)
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        filter_string,
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
        str(output_path),
    ]

    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    elapsed = round(time.monotonic() - started, 3)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "Running FFmpeg: " + " ".join(command) + "\n\n" + result.stdout + result.stderr,
            encoding="utf-8",
        )
    if result.returncode != 0:
        raise PostprocessError(f"FFmpeg postprocess failed with exit code {result.returncode}. See {log_path}.")
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise PostprocessError(f"Postprocessed output was not created: {output_path}")

    metadata = {
        "preset": preset.name,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "filter_string": filter_string,
        "elapsed_seconds": elapsed,
        "ffmpeg_command": command,
    }
    output_path.with_suffix(".postprocess.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata
