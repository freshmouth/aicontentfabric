from __future__ import annotations

from typing import Any

from .subject import SubjectDescriptor, replace_product_placeholder


# v3: dynamic subject support
MASTER_UGC_IMAGE_PROMPT = """Create one vertical 9:16 first-frame image for a short-form UGC video.

The image must feel like a real paused frame from the upcoming clip, not a poster.

Primary physical subject:
[PRODUCT]

The [PRODUCT] must be visible, physically plausible, and matched to the scene action.

Style:
real smartphone frame, authentic UGC, documentary realism, natural imperfections, believable hands, believable household or retail environment.

Avoid:
floating objects, impossible hand contact, fake labels, unreadable prompt text on packaging, social UI, captions, subtitles, watermarks, polished ad lighting."""


def build_image_prompt(
    scene: dict[str, Any],
    *,
    project: dict[str, Any] | None = None,
    master_template: str = MASTER_UGC_IMAGE_PROMPT,
) -> str:
    subject = SubjectDescriptor.from_mapping(project, scene)
    scene_prompt = str(scene.get("prompt") or scene.get("visual") or "").strip()
    dialogue = str(scene.get("dialogue") or scene.get("script") or "").strip()
    role = str(scene.get("role") or "").strip()
    parts = [
        replace_product_placeholder(master_template, subject),
        f"Subject placement hint: {subject.placement_hint}.",
        f"Scene role: {role}." if role else "",
        f"Scene instruction: {scene_prompt}" if scene_prompt else "",
        f"Native dialogue context: {dialogue}" if dialogue else "",
    ]
    return "\n\n".join(part for part in parts if part)
