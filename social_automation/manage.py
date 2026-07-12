from __future__ import annotations

import argparse
import json
from pathlib import Path

from .campaigns import CampaignRegistry
from .models import CommentEvent


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and test Meta comment campaigns.")
    parser.add_argument("--campaigns", default="social_automation/campaigns.json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    simulate = subparsers.add_parser("simulate")
    simulate.add_argument("--platform", choices=("instagram", "facebook"), required=True)
    simulate.add_argument("--media-id", required=True)
    simulate.add_argument("--comment", required=True)
    args = parser.parse_args()

    registry = CampaignRegistry(Path(args.campaigns))
    campaigns = registry.campaigns()
    if args.command == "validate":
        print(json.dumps({"valid": True, "campaign_count": len(campaigns), "enabled": sum(c.enabled for c in campaigns)}, indent=2))
        return 0

    event = CommentEvent(platform=args.platform, comment_id="simulation", media_id=args.media_id, text=args.comment)
    match = registry.match(event)
    if not match:
        print(json.dumps({"matched": False}, indent=2))
        return 1
    campaign, keyword = match
    print(json.dumps({"matched": True, "campaign_id": campaign.campaign_id, "keyword": keyword, "message": campaign.render_private_message(event, keyword)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

