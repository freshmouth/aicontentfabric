from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class ChatRequest(BaseModel):
    account_id: str
    message: str = Field(min_length=3, max_length=12000)
    draft_id: str | None = None


class DraftUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    brief: str | None = Field(default=None, max_length=12000)
    caption: str | None = Field(default=None, max_length=4000)
    status: Literal["draft", "approved", "archived"] | None = None
    creative_spec: dict[str, Any] | None = None


class GenerateRequest(BaseModel):
    publish_at: str | None = None
    dry_run: bool = True
    skip_publish: bool = False

    @field_validator("publish_at")
    @classmethod
    def validate_publish_at(cls, value: str | None) -> str | None:
        if not value:
            return None
        datetime.fromisoformat(value)
        return value


class AccountScheduleUpdate(BaseModel):
    enabled: bool | None = None
    interval_days: int | None = Field(default=None, ge=1, le=365)
    publish_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str | None = Field(default=None, min_length=3, max_length=80)


class DraftRecord(BaseModel):
    id: str
    account_id: str
    title: str
    brief: str
    caption: str
    status: Literal["draft", "approved", "archived"] = "draft"
    creative_spec: dict[str, Any]
    chat_history: list[dict[str, str]] = Field(default_factory=list)
    version: int = 1
    created_at: str
    updated_at: str


class JobRecord(BaseModel):
    id: str
    account_id: str
    draft_id: str
    concept_id: str
    status: Literal["queued", "in_progress", "succeeded", "failed", "cancelled"] = "queued"
    publish_at: str | None = None
    dry_run: bool = True
    skip_publish: bool = False
    github_run_id: int | None = None
    github_run_url: str | None = None
    error_code: str | None = None
    created_at: str
    updated_at: str
