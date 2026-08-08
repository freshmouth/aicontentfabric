from __future__ import annotations

from typing import Any

from .subject import SubjectDescriptor


# v3: dynamic subject support. Keep the JSON key product_placement stable.
QA_RESULT_SCHEMA = {
    "product_placement": "number 1-10",
    "hand_realism": "number 1-10",
    "face_quality": "number 1-10",
    "background_realism": "number 1-10",
    "ugc_authenticity": "number 1-10",
    "status": "PASS or FAIL",
    "issues": ["short issue strings"],
}


def build_qa_prompt(
    image_prompt: str,
    *,
    project: dict[str, Any] | None = None,
    scene: dict[str, Any] | None = None,
) -> str:
    subject = SubjectDescriptor.from_mapping(project, scene)
    return f"""Evaluate the generated image against the requested UGC first-frame prompt.

Return strict JSON with these exact keys:
- product_placement
- hand_realism
- face_quality
- background_realism
- ugc_authenticity
- status
- issues

The `issues` value MUST be a JSON array of critical physical errors. Return []
when there are no critical errors. Do not include optional improvements there.

Scoring rubric, 1-10:
1. product_placement: the {subject.label} is {subject.placement_hint}; fail if the {subject.label} floats, is detached from the hand/surface, appears physically impossible, is missing, or is replaced by the wrong subject.
2. hand_realism: hands and fingers are anatomically plausible and interact naturally with the scene.
3. face_quality: any visible face looks realistic, stable, and not waxy or distorted.
4. background_realism: the environment matches the requested scene and feels physically real.
5. ugc_authenticity: the frame feels like an authentic paused UGC video, not a polished ad.

Camera and hand logic:
- A selfie or front-facing phone-camera frame is photographed by a phone outside the image. Never require that capture phone to be visible or held inside its own frame.
- If the prompt separately requests a desk phone or another device, judge that prop only in its requested location.
- If the prompt does not require visible hands and no hands are visible, score hand_realism as 10 unless an anatomical artifact is actually present. Never invent a missing-hand failure just to populate this score.

PASS only when all five scores are 8 or higher and there are no critical physical errors.

Requested image prompt:
{image_prompt}
"""


def build_fixed_prompt(
    original_prompt: str,
    qa_result: dict[str, Any],
    *,
    project: dict[str, Any] | None = None,
    scene: dict[str, Any] | None = None,
) -> str:
    subject = SubjectDescriptor.from_mapping(project, scene)
    issues_text = " ".join(str(issue).lower() for issue in qa_result.get("issues") or [])
    placement_score = _score(qa_result.get("product_placement"))
    fixes: list[str] = []
    if placement_score < 8 or any(
        word in issues_text
        for word in ("floating", "detached", "not connected", "missing", "wrong subject", "impossible")
    ):
        fixes.append(
            "CRITICAL: The "
            f"{subject.label} MUST be {subject.placement_hint}. The {subject.label} cannot float, "
            "cannot be detached from the hand or surface, cannot be replaced by another object, "
            "and must obey normal physical contact."
        )
    if _score(qa_result.get("hand_realism")) < 8 or "hand" in issues_text or "finger" in issues_text:
        fixes.append(
            "CRITICAL: Make all hands and fingers anatomically plausible and naturally connected to the action."
        )
    if _score(qa_result.get("face_quality")) < 8 or "face" in issues_text:
        fixes.append("CRITICAL: Keep any visible face realistic, stable, and free of waxy or distorted features.")
    if _score(qa_result.get("background_realism")) < 8 or "background" in issues_text:
        fixes.append("CRITICAL: Keep the environment physically realistic and matched to the scene.")
    if _score(qa_result.get("ugc_authenticity")) < 8 or "ad" in issues_text:
        fixes.append("CRITICAL: Make the frame feel like a candid paused UGC video, not an advertisement.")
    if not fixes:
        return original_prompt
    return original_prompt.rstrip() + "\n\n" + "\n".join(fixes)


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
