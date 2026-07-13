from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .characters import CharacterProfile, load_character


class FirstFrameError(RuntimeError):
    pass


BANNED_PROP_LINE = (
    "Forbidden carryover props: orange, malformed orange, citrus fruit, bananas, fruit bowl, grocery bag, salad dressing, "
    "salad greens, fake wellness jar, creator-branded package, ebook, text card, subtitles, watermark, social UI."
)


def prepare_omni_v11_first_frames(
    omni_config: dict[str, Any],
    runtime_config: dict[str, Any],
    *,
    work_dir: Path,
    root: Path,
) -> dict[str, Any]:
    """Generate scene-owned first frames and wire them as Omni references.

    v1.1 deliberately does not pass a full character master image into Omni. Instead,
    we derive a face-only identity crop from the selected character's canonical image,
    generate one clean first frame per leaf scene, then use each first frame as the
    sole video reference for that leaf.
    """
    api_key_env = str(runtime_config.get("openai_api_key_env") or "OPENAI_API_KEY")
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise FirstFrameError(f"{api_key_env} is required for daily_factory_v1_1 first-frame generation.")

    output_dir = work_dir / "first_frames"
    output_dir.mkdir(parents=True, exist_ok=True)
    character = load_character(root, runtime_config, dict(omni_config.get("concept") or {}))
    reference_image = resolve_path(root, character.master_reference)
    identity_crop = output_dir / f"{character.character_id}_identity_face.png"
    ensure_identity_crop(reference_image, identity_crop, ffmpeg_path=str(runtime_config.get("ffmpeg_path") or "ffmpeg"))

    openai_config = dict(runtime_config.get("openai") or {})
    image_settings = {
        "model": str(openai_config.get("image_model") or "gpt-image-2"),
        "size": str(openai_config.get("image_size") or "720x1280"),
        "quality": str(openai_config.get("image_quality") or "medium"),
        "timeout": int(openai_config.get("timeout_seconds") or 240),
        "ffmpeg_path": str(runtime_config.get("ffmpeg_path") or "ffmpeg"),
    }

    manifest: list[dict[str, Any]] = []
    for leaf in iter_leaf_scenes(omni_config):
        first_frame_path = output_dir / f"{leaf['role']}_{leaf['component_id']}_{leaf['segment_id']}.png"
        product_only = is_product_only(leaf["node"])
        prompt = build_first_frame_prompt(
            scene_id=f"{leaf['role']}:{leaf['component_id']}:{leaf['segment_id']}",
            leaf_prompt=str(leaf["node"].get("prompt") or ""),
            product_only=product_only,
            character=character,
        )
        prompt_path = first_frame_path.with_suffix(".prompt.txt")
        prompt_path.write_text(prompt, encoding="utf-8")
        refs = [] if product_only else [identity_crop]
        generate_openai_first_frame(
            prompt=prompt,
            output_path=first_frame_path,
            reference_images=refs,
            api_key=api_key,
            settings=image_settings,
        )
        leaf["node"]["reference_images"] = [str(first_frame_path)]
        manifest.append(
            {
                "role": leaf["role"],
                "component_id": leaf["component_id"],
                "segment_id": leaf["segment_id"],
                "product_only": product_only,
                "prompt_path": str(prompt_path),
                "first_frame": str(first_frame_path),
                "identity_crop_used": str(identity_crop) if refs else "",
                "character_id": character.character_id,
            }
        )

    for key in ("meals", "mains"):
        for component in omni_config.get(key, []) or []:
            if isinstance(component, dict) and component.get("segments"):
                component["reference_images"] = []

    omni_config["recipe_id"] = "daily_factory_v1_1"
    omni_config["first_frame_source"] = {
        "provider": "openai_images",
        "mode": "scene_first_frame_to_omni_reference",
        "character_id": character.character_id,
        "identity_crop": str(identity_crop),
        "manifest": str(output_dir / "first_frame_manifest.json"),
    }
    write_json(output_dir / "first_frame_manifest.json", manifest)
    return omni_config


def iter_leaf_scenes(config: dict[str, Any]) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []
    for role_key, role in (("hooks", "hooks"), ("ctas", "ctas"), ("desserts", "ctas"), ("closings", "ctas")):
        for component in config.get(role_key, []) or []:
            if not isinstance(component, dict):
                continue
            cid = slug(str(component.get("id") or component.get("title") or role))
            leaves.append({"role": role, "component_id": cid, "segment_id": cid, "node": component})
    for role_key in ("meals", "mains"):
        for component in config.get(role_key, []) or []:
            if not isinstance(component, dict):
                continue
            cid = slug(str(component.get("id") or component.get("title") or role_key))
            segments = component.get("segments") if isinstance(component.get("segments"), list) else []
            if not segments:
                leaves.append({"role": "mains", "component_id": cid, "segment_id": cid, "node": component})
                continue
            for index, segment in enumerate(segments, start=1):
                if not isinstance(segment, dict):
                    continue
                sid = slug(str(segment.get("id") or f"{cid}_{index:02d}"))
                leaves.append({"role": "mains", "component_id": cid, "segment_id": sid, "node": segment})
    return leaves


def is_product_only(node: dict[str, Any]) -> bool:
    if "reference_images" in node and not node.get("reference_images"):
        return True
    prompt = str(node.get("prompt") or "").lower()
    return "product-only" in prompt or "no face visible" in prompt or "no selfie framing" in prompt


def build_first_frame_prompt(*, scene_id: str, leaf_prompt: str, product_only: bool, character: CharacterProfile) -> str:
    identity_line = (
        f"If a face identity reference is attached, use it only for {character.name}'s face, age, eyes, skin texture, and hair. "
        "Do not copy its room, clothing, hands, fruit, orange, background objects, or props."
    )
    if product_only:
        identity_line = f"This is product-only B-roll. Do not show {character.name}, a face, a selfie, or any human body except the single hand if requested."
    return "\n".join(
        [
            "Create one hyper-realistic 9:16 vertical first frame for a UGC health reel clip.",
            "This image will be used as the exact visual source for video generation, so it must be clean, stable, and scene-specific.",
            f"Character lock: {character.prompt_identity()}",
            identity_line,
            "Use only the objects named in this scene prompt. Do not import props from any other reference image or previous video.",
            BANNED_PROP_LINE,
            "No text overlays, no captions, no watermark, no social UI, no fake CTA card.",
            f"Use real supermarket product cues when a product is named. Never make a {character.name}-branded product.",
            "Keep the frame candid and smartphone-realistic, but avoid awkward hallucinated hands, floating products, or utensils stuck through objects.",
            f"Scene id: {scene_id}",
            "Scene prompt:",
            leaf_prompt.strip(),
        ]
    )


def generate_openai_first_frame(
    *,
    prompt: str,
    output_path: Path,
    reference_images: list[Path],
    api_key: str,
    settings: dict[str, Any],
) -> None:
    response = (
        submit_openai_image_edit(api_key, prompt, reference_images, settings)
        if reference_images
        else submit_openai_image_generation(api_key, prompt, settings)
    )
    data = response.get("data") if isinstance(response, dict) else None
    if not data or not isinstance(data, list):
        raise FirstFrameError("OpenAI image response did not include data.")
    b64_image = data[0].get("b64_json") if isinstance(data[0], dict) else ""
    if not b64_image:
        raise FirstFrameError("OpenAI image response did not include b64_json.")
    raw_path = output_path.with_name(output_path.stem + ".openai_raw.png")
    raw_path.write_bytes(base64.b64decode(str(b64_image)))
    normalize_image(raw_path, output_path, ffmpeg_path=str(settings["ffmpeg_path"]))
    raw_path.unlink(missing_ok=True)


def submit_openai_image_edit(
    api_key: str,
    prompt: str,
    reference_images: list[Path],
    settings: dict[str, Any],
) -> dict[str, Any]:
    fields = {
        "model": str(settings["model"]),
        "prompt": prompt,
        "size": str(settings["size"]),
        "quality": str(settings["quality"]),
        "output_format": "png",
    }
    body, content_type = build_multipart_form_data(fields, reference_images)
    request = urllib.request.Request(
        "https://api.openai.com/v1/images/edits",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": content_type},
    )
    return open_json(request, timeout=int(settings["timeout"]))


def submit_openai_image_generation(api_key: str, prompt: str, settings: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(
        {
            "model": str(settings["model"]),
            "prompt": prompt,
            "size": str(settings["size"]),
            "quality": str(settings["quality"]),
            "output_format": "png",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    return open_json(request, timeout=int(settings["timeout"]))


def build_multipart_form_data(fields: dict[str, str], image_paths: list[Path]) -> tuple[bytes, str]:
    boundary = f"----daily-factory-v11-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for path in image_paths:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="image[]"; filename="{path.name}"\r\n'.encode("utf-8"),
                b"Content-Type: image/png\r\n\r\n",
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def ensure_identity_crop(source: Path, output_path: Path, *, ffmpeg_path: str) -> None:
    if not source.exists() or source.stat().st_size <= 0:
        raise FirstFrameError(f"Missing character master reference: {source}")
    if output_path.exists() and output_path.stat().st_size > 0:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path,
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vf",
        "crop=w=min(iw\\,760):h=min(ih\\,820):x=0:y=0,scale=720:-2",
        "-frames:v",
        "1",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
        raise FirstFrameError(f"Could not create identity crop: {result.stderr[-1200:]}")


def normalize_image(input_path: Path, output_path: Path, *, ffmpeg_path: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg_path,
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1",
            "-frames:v",
            "1",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
        raise FirstFrameError(f"Could not normalize first frame: {result.stderr[-1200:]}")


def open_json(request: urllib.request.Request, *, timeout: int) -> dict[str, Any]:
    try:
        with open_url(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FirstFrameError(f"OpenAI image request failed: HTTP {exc.code}: {detail[:2000]}") from exc
    except urllib.error.URLError as exc:
        raise FirstFrameError(f"OpenAI image request failed: {exc.reason}") from exc


def open_url(request: urllib.request.Request, *, timeout: int):
    if os.environ.get("OPENAI_SSL_NO_VERIFY", "").strip().lower() in {"1", "true", "yes"}:
        return urllib.request.urlopen(request, timeout=timeout, context=ssl._create_unverified_context())
    return urllib.request.urlopen(request, timeout=timeout)


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def slug(value: str) -> str:
    chars: list[str] = []
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
        elif chars and chars[-1] != "_":
            chars.append("_")
    return "".join(chars).strip("_") or "scene"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
