from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any

import truststore

from .creative_contract import CreativeContractError, compile_source_config


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
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise CreativeServiceError("OPENAI_API_KEY is not configured for dashboard creative chat.")
        template = account_context["source_config"]
        attachments = attachments or []
        prompt = self._prompt(account_context, message, current_draft, attachments)
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        content.extend(
            {"type": "input_image", "image_url": item["data_url"], "detail": "high"}
            for item in attachments
        )
        payload = {
            "model": self.model,
            "input": [{"role": "user", "content": content}],
            "text": {"format": {"type": "json_object"}},
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
                timeout=100,
                context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise CreativeServiceError(f"OpenAI creative request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CreativeServiceError(f"OpenAI creative request failed: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise CreativeServiceError(
                "OpenAI creative request timed out after 100 seconds. Nothing was queued; please retry."
            ) from exc
        parsed = parse_json_response(extract_response_text(raw))
        spec = parsed.get("source_config")
        if not isinstance(spec, dict):
            raise CreativeServiceError("Creative response did not contain source_config.")
        spec["account_id"] = account_context["account"]["account_id"]
        spec.setdefault("language", template.get("language") or "en")
        try:
            compiled, report = compile_source_config(spec)
        except CreativeContractError as exc:
            raise CreativeServiceError(str(exc)) from exc
        parsed["source_config"] = compiled
        parsed["creative_preflight"] = report
        return parsed

    @staticmethod
    def _prompt(
        context: dict[str, Any],
        message: str,
        current: dict[str, Any] | None,
        attachments: list[dict[str, str]],
    ) -> str:
        account = context["account"]
        template = context["source_config"]
        current_json = json.dumps((current or {}).get("creative_spec") or {}, ensure_ascii=False)
        conversation = [
            {
                "role": item.get("role"),
                "content": item.get("content"),
            }
            for item in ((current or {}).get("chat_history") or [])[-16:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        conversation_json = json.dumps(conversation, ensure_ascii=False)
        attachment_names = [item.get("filename") or "reference image" for item in attachments]
        return f"""
You are an opinionated, collaborative AI creative director inside a multi-account short-form video factory.
Talk with the user like a senior creative partner: respond directly, explain meaningful choices, point out risks,
and propose concrete next moves. Do not behave like a form processor or merely announce that JSON was updated.

ACCOUNT ISOLATION IS ABSOLUTE.
Account id: {account['account_id']}
Account name: {account['display_name']}
Account description: {account['description']}
Language: {template.get('language', 'en')}

Use the supplied V3 template as structural and identity guidance. Never borrow a person, product, CTA, room,
brand voice, or asset from another account. Create one coherent vertical UGC video with independent leaf scenes,
one exact spoken line per scene, no repeated line fragments, no montage inside a scene, no baked captions, and a
clear ending. Return at least one hook, one CTA, and enough chronological main leaf scenes to execute the user's
creative without an arbitrary scene-count ceiling. Keep health claims educational and non-medical. Preserve the
template's character and visual rules.

GOOGLE OMNI EXECUTION CONTRACT:
Every leaf scene must be 3-10 seconds. Size the exact spoken line so it finishes naturally before the scene ends,
including a short safety margin; use roughly 2.35 spoken words per second as the upper pacing limit. If a thought
needs more than 10 seconds, split it into chronological independent leaf scenes with distinct visuals and no
repeated opening or closing words. Every leaf needs one visual action, one location, one speaker, one exact spoken
line, and a non-empty prompt. The attached/generated first frame is the visual source of truth for that leaf.

USER REQUEST:
{message}

CONVERSATION HISTORY:
{conversation_json}

Use the conversation history to understand short follow-ups such as "show me", "make it sharper", or "use the
second image". If the user asks to see, explain, compare, critique, or review the current draft, answer that request
in detail and preserve the current source_config unless they also request a revision. If they request a revision,
state what changed and why. Never claim generation, rendering, or publishing has happened inside this chat.

ATTACHED VISUAL REFERENCES:
{json.dumps(attachment_names, ensure_ascii=False)}
Treat attached photos as real visual inputs. Follow the user's instruction for whether each image is a character,
product, aesthetic, location, composition, or storyboard reference. Describe the relevant visual facts inside the
scene prompts so downstream image and video generation can use them. Do not import identity or branding from any
other account, and do not claim an attachment was used when none is listed.

CURRENT DRAFT, if this is a revision:
{current_json}

REFERENCE V3 TEMPLATE:
{json.dumps(template, ensure_ascii=False)}

Return only valid JSON with this shape:
{{
  "assistant_message": "a conversational and useful response that directly answers the user; include the actual hook, script, scene arc, critique, or options when relevant rather than a generic confirmation",
  "suggested_actions": ["2-4 concise follow-up commands tailored to this draft"],
  "execution_request": {{
    "action": "none | generate_only | generate_and_publish",
    "publish_at": null
  }},
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
    "hooks": [{{"id":"...","title":"...","hook_text":"direct scroll-stopping overlay under 8 words","duration_seconds":4,"subject_label":"...","subject_placement_hint":"...","script":"...","prompt":"... Native dialogue: ..."}}],
    "mains": [{{"id":"...","title":"...","prompt":"...","segments":[{{"id":"...","duration_seconds":5,"subject_label":"...","subject_placement_hint":"...","script":"...","prompt":"... Native dialogue: ..."}}]}}],
    "ctas": [{{"id":"...","title":"...","duration_seconds":5,"subject_label":"...","subject_placement_hint":"...","script":"...","prompt":"... Native dialogue: ..."}}],
    "variants": {{"count":1,"min_total_seconds":25,"max_total_seconds":45,"seed":20260804,"stitch_leaf_segments":true}}
  }}
}}

Set execution_request.action to generate_only only when the latest user message explicitly asks to generate,
render, assemble, run, or proceed with the video. Set it to generate_and_publish only when the latest message
explicitly asks to publish, post, send to Metricool, or schedule the finished video. Otherwise use none. Never claim
an execution succeeded; the control plane performs and reports that action after your response is validated.
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
    try:
        compile_source_config(spec)
    except CreativeContractError as exc:
        raise CreativeServiceError(str(exc)) from exc
