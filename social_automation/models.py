from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommentEvent:
    platform: str
    comment_id: str
    text: str
    media_id: str = ""
    author_id: str = ""
    author_name: str = ""
    raw: dict[str, Any] | None = None

    @property
    def event_key(self) -> str:
        return f"{self.platform}:{self.comment_id}"

