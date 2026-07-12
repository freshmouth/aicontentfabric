from __future__ import annotations

from typing import Any

from .models import CommentEvent


def extract_comment_events(payload: dict[str, Any]) -> list[CommentEvent]:
    object_type = str(payload.get("object") or "").lower()
    if object_type == "instagram":
        return _extract_instagram(payload)
    if object_type == "page":
        return _extract_facebook(payload)
    return []


def _extract_instagram(payload: dict[str, Any]) -> list[CommentEvent]:
    events: list[CommentEvent] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if str(change.get("field") or "").lower() not in {"comments", "live_comments"}:
                continue
            value = change.get("value") or {}
            comment_id = str(value.get("id") or value.get("comment_id") or "").strip()
            text = str(value.get("text") or value.get("message") or "").strip()
            if not comment_id or not text:
                continue
            author = value.get("from") or {}
            media = value.get("media") or {}
            events.append(
                CommentEvent(
                    platform="instagram",
                    comment_id=comment_id,
                    text=text,
                    media_id=str(media.get("id") or value.get("media_id") or "").strip(),
                    author_id=str(author.get("id") or value.get("from_id") or "").strip(),
                    author_name=str(author.get("username") or author.get("name") or "").strip(),
                    raw=value,
                )
            )
    return events


def _extract_facebook(payload: dict[str, Any]) -> list[CommentEvent]:
    events: list[CommentEvent] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if str(change.get("field") or "").lower() != "feed":
                continue
            value = change.get("value") or {}
            if str(value.get("item") or "").lower() != "comment":
                continue
            if str(value.get("verb") or "add").lower() != "add":
                continue
            comment_id = str(value.get("comment_id") or value.get("id") or "").strip()
            text = str(value.get("message") or value.get("text") or "").strip()
            if not comment_id or not text:
                continue
            author = value.get("from") or {}
            events.append(
                CommentEvent(
                    platform="facebook",
                    comment_id=comment_id,
                    text=text,
                    media_id=str(value.get("post_id") or value.get("parent_id") or "").strip(),
                    author_id=str(author.get("id") or "").strip(),
                    author_name=str(author.get("name") or "").strip(),
                    raw=value,
                )
            )
    return events

