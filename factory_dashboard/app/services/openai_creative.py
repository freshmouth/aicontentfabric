from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from typing import Any

import truststore


class CreativeServiceError(RuntimeError):
    pass


class OpenAICreativeService:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def create_or_revise(
        self,
        *,
        account_context: dict[str, Any],
        message: str,
        current_draft: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise CreativeServiceError("OPENAI_API_KEY is not configured for dashboard creative chat.")
        template = account_context["source_config"]
        prompt = self._prompt(account_context, message, current_draft)
        payload = {
            "model": self.model,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            "max_output_tokens": 7000,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=240,
                context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise CreativeServiceError(f"OpenAI creative request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CreativeServiceError(f"OpenAI creative request failed: {exc.reason}") from exc
        parsed = parse_json_response(extract_response_text(raw))
        spec = parsed.get("source_config")
        if not isinstance(spec, dict):
            raise CreativeServiceError("Creative response did not contain source_config.")
        spec["account_id"] = account_context["account"]["account_id"]
        spec.setdefault("language", template.get("language") or "en")
        validate_source_config(spec)
        return parsed

    @staticmethod
    def _prompt(context: dict[str, Any], message: str, current: dict[str, Any] | None) -> str:
        account = context["account"]
        template = context["source_config"]
        current_json = json.dumps((current or {}).get("creative_spec") or {}, ensure_ascii=False)
        return f"""
You are the creative director inside a multi-account short-form video factory.

ACCOUNT ISOLATION IS ABSOLUTE.
Account id: {account['account_id']}
Account name: {account['display_name']}
Account description: {account['description']}
Language: {template.get('language', 'en')}

Use the supplied V3 template as structural and identity guidance. Never borrow a person, product, CTA, room,
brand voice, or asset from another account. Create one coherent vertical UGC video with independent leaf scenes,
one exact spoken line per scene, no repeated line fragments, no montage inside a scene, no baked captions, and a
clear ending. Keep health claims educational and non-medical. Preserve the template's character and visual rules.

USER REQUEST:
{message}

CURRENT DRAFT, if this is a revision:
{current_json}

REFERENCE V3 TEMPLATE:
{json.dumps(template, ensure_ascii=False)}

Return only valid JSON with this shape:
{{
  "assistant_message": "short explanation of what changed",
  "title": "working title",
  "brief": "concise creative brief",
  "caption": "short contextual social caption with the same CTA but not a transcript",
  "source_config": {{
    "name": "...",
    "concept_id": "lowercase_slug",
    "account_id": "{account['account_id']}",
    "language": "...",
    "defaults": {{"aspect_ratio": "9:16", "duration_seconds": 5, "resolution": "720p"}},
    "master_prompt": "account-specific consistency and UGC rules",
    "hooks": [{{"id":"...","title":"...","duration_seconds":4,"subject_label":"...","subject_placement_hint":"...","script":"...","prompt":"... Native dialogue: ..."}}],
    "mains": [{{"id":"...","title":"...","prompt":"...","segments":[{{"id":"...","duration_seconds":5,"subject_label":"...","subject_placement_hint":"...","script":"...","prompt":"... Native dialogue: ..."}}]}}],
    "ctas": [{{"id":"...","title":"...","duration_seconds":5,"subject_label":"...","subject_placement_hint":"...","script":"...","prompt":"... Native dialogue: ..."}}],
    "variants": {{"count":1,"min_total_seconds":25,"max_total_seconds":45,"seed":20260804,"stitch_leaf_segments":true}}
  }}
}}
""".strip()


def extract_response_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    parts: list[str] = []
    for item in response.get("output", []) or []:
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    if not parts:
        raise CreativeServiceError("OpenAI response did not contain text.")
    return "\n".join(parts)


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CreativeServiceError("Creative response was not valid JSON.") from exc
    if not isinstance(value, dict):
        raise CreativeServiceError("Creative response must be a JSON object.")
    return value


def validate_source_config(spec: dict[str, Any]) -> None:
    for key in ("concept_id", "account_id", "master_prompt", "hooks", "mains", "ctas"):
        if not spec.get(key):
            raise CreativeServiceError(f"Creative source_config is missing {key}.")
    scene_count = len(spec["hooks"]) + len(spec["ctas"])
    scene_count += sum(len(item.get("segments") or [item]) for item in spec["mains"])
    if not 3 <= scene_count <= 12:
        raise CreativeServiceError("Creative source_config must contain 3-12 leaf scenes.")
