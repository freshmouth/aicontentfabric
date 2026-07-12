from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import CommentEvent


class CampaignError(RuntimeError):
    pass


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    keywords: tuple[str, ...]
    platforms: tuple[str, ...]
    media_ids: dict[str, tuple[str, ...]]
    private_message: str
    public_reply: str
    asset_url: str
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Campaign":
        campaign_id = str(raw.get("campaign_id") or "").strip()
        keywords = tuple(str(value).strip() for value in raw.get("keywords", []) if str(value).strip())
        private_message = str(raw.get("private_message") or "").strip()
        if not campaign_id or not keywords or not private_message:
            raise CampaignError("Each campaign requires campaign_id, keywords, and private_message.")
        media_raw = raw.get("media_ids") or {}
        media_ids = {
            str(platform).lower(): tuple(str(value).strip() for value in values if str(value).strip())
            for platform, values in media_raw.items()
        }
        return cls(
            campaign_id=campaign_id,
            keywords=keywords,
            platforms=tuple(str(value).lower() for value in raw.get("platforms", ["instagram", "facebook"])),
            media_ids=media_ids,
            private_message=private_message,
            public_reply=str(raw.get("public_reply") or "").strip(),
            asset_url=str(raw.get("asset_url") or "").strip(),
            enabled=bool(raw.get("enabled", True)),
        )

    def matches(self, event: CommentEvent) -> str | None:
        if not self.enabled or event.platform not in self.platforms:
            return None
        allowed_media = self.media_ids.get(event.platform, ("*",))
        if allowed_media and "*" not in allowed_media and event.media_id not in allowed_media:
            return None
        for keyword in self.keywords:
            pattern = rf"(?<!\w){re.escape(keyword)}(?!\w)"
            if re.search(pattern, event.text, flags=re.IGNORECASE):
                return keyword
        return None

    def render_private_message(self, event: CommentEvent, keyword: str) -> str:
        values = {
            "asset_url": self.asset_url,
            "keyword": keyword,
            "first_name": event.author_name.split()[0] if event.author_name else "there",
        }
        message = self.private_message.format_map(values).strip()
        if self.asset_url and self.asset_url not in message:
            message = f"{message}\n\n{self.asset_url}"
        return message


class CampaignRegistry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime_ns = -1
        self._campaigns: tuple[Campaign, ...] = ()

    def campaigns(self) -> tuple[Campaign, ...]:
        if not self.path.exists():
            raise CampaignError(f"Campaign registry does not exist: {self.path}")
        mtime_ns = self.path.stat().st_mtime_ns
        if mtime_ns != self._mtime_ns:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw.get("campaigns") if isinstance(raw, dict) else raw
            if not isinstance(rows, list):
                raise CampaignError("Campaign registry must contain a campaigns array.")
            self._campaigns = tuple(Campaign.from_dict(row) for row in rows)
            self._mtime_ns = mtime_ns
        return self._campaigns

    def match(self, event: CommentEvent) -> tuple[Campaign, str] | None:
        for campaign in self.campaigns():
            keyword = campaign.matches(event)
            if keyword:
                return campaign, keyword
        return None

