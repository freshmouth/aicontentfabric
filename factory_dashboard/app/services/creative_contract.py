from __future__ import annotations

import copy
import math
import re
from typing import Any


OMNI_MIN_DURATION_SECONDS = 3
OMNI_MAX_DURATION_SECONDS = 10
SAFE_SPOKEN_WORDS_PER_SECOND = 2.35
SPEECH_END_MARGIN_SECONDS = 0.6


class CreativeContractError(ValueError):
    pass


def compile_source_config(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a provider-safe copy and a report before any paid generation starts."""
    if not isinstance(spec, dict):
        raise CreativeContractError("Creative source_config must be an object.")
    compiled = copy.deepcopy(spec)
    for key in ("concept_id", "account_id", "master_prompt", "hooks", "mains", "ctas"):
        if not compiled.get(key):
            raise CreativeContractError(f"Creative source_config is missing {key}.")
    for key in ("hooks", "mains", "ctas"):
        if not isinstance(compiled[key], list):
            raise CreativeContractError(f"Creative source_config {key} must be a list.")

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for role, parent, leaf, index in iter_leaf_scenes(compiled):
        scene_id = str(leaf.get("id") or parent.get("id") or f"{role}_{index:02d}").strip()
        if not scene_id:
            scene_id = f"{role}_{index:02d}"
        if scene_id in seen_ids:
            errors.append(f"Duplicate leaf scene id: {scene_id}.")
        seen_ids.add(scene_id)
        leaf["id"] = scene_id

        prompt = str(leaf.get("prompt") or parent.get("prompt") or "").strip()
        script = str(leaf.get("script") or parent.get("script") or "").strip()
        if not prompt:
            errors.append(f"Scene {scene_id} has no visual prompt.")
        if not script:
            errors.append(f"Scene {scene_id} has no exact spoken line.")
        if re.search(r"\$\s*\d", script) and not re.search(
            r"\b(pesos?|d[oó]lares?|usd|mxn|cad|aud|eur|euros?)\b", script, flags=re.IGNORECASE
        ):
            errors.append(
                f"Scene {scene_id} uses an ambiguous currency symbol. Write the spoken currency unit explicitly."
            )

        requested = parse_duration(
            leaf.get("duration_seconds"),
            parent.get("duration_seconds"),
            dict(compiled.get("defaults") or {}).get("duration_seconds"),
        )
        words = spoken_word_count(script)
        speech_minimum = max(
            OMNI_MIN_DURATION_SECONDS,
            int(math.ceil((words / SAFE_SPOKEN_WORDS_PER_SECOND) + SPEECH_END_MARGIN_SECONDS)),
        )
        normalized = max(OMNI_MIN_DURATION_SECONDS, requested, speech_minimum)
        if normalized > OMNI_MAX_DURATION_SECONDS:
            errors.append(
                f"Scene {scene_id} needs about {normalized}s for {words} spoken words, but Omni allows at most "
                f"{OMNI_MAX_DURATION_SECONDS}s. Split its dialogue into independent leaf scenes."
            )
            normalized = OMNI_MAX_DURATION_SECONDS
        leaf["duration_seconds"] = normalized
        records.append(
            {
                "index": index,
                "role": role,
                "scene_id": scene_id,
                "requested_duration_seconds": requested,
                "duration_seconds": normalized,
                "duration_adjusted": requested != normalized,
                "spoken_word_count": words,
                "speech_minimum_seconds": speech_minimum,
                "prompt_present": bool(prompt),
                "script_present": bool(script),
            }
        )

    if len(records) < 3:
        errors.append("Creative source_config must contain at least 3 leaf scenes.")
    if errors:
        raise CreativeContractError("Creative preflight failed: " + " ".join(errors))

    total_duration = sum(int(record["duration_seconds"]) for record in records)
    variants = compiled.setdefault("variants", {})
    variants["count"] = 1
    variants["min_total_seconds"] = max(0.1, total_duration - 0.01)
    variants["max_total_seconds"] = total_duration + 0.01
    variants["stitch_leaf_segments"] = True
    defaults = compiled.setdefault("defaults", {})
    defaults.setdefault("aspect_ratio", "9:16")
    defaults.setdefault("duration_seconds", 5)
    defaults.setdefault("resolution", "720p")
    report = {
        "status": "passed",
        "provider": "google_omni_flash",
        "scene_count": len(records),
        "total_duration_seconds": total_duration,
        "adjusted_scene_count": sum(bool(record["duration_adjusted"]) for record in records),
        "scenes": records,
    }
    return compiled, report


def iter_leaf_scenes(config: dict[str, Any]):
    index = 0
    for role in ("hooks", "mains", "meals", "ctas", "desserts", "closings"):
        for component in list(config.get(role) or []):
            if not isinstance(component, dict):
                continue
            segments = component.get("segments")
            leaves = segments if isinstance(segments, list) and segments else [component]
            for leaf in leaves:
                if not isinstance(leaf, dict):
                    continue
                index += 1
                yield role, component, leaf, index


def parse_duration(*values: Any) -> int:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = int(round(float(value)))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 5


def spoken_word_count(script: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", script, flags=re.UNICODE))
