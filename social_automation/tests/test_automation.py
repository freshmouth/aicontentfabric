from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from social_automation.campaigns import CampaignRegistry
from social_automation.service import create_app
from social_automation.store import MemoryDeliveryStore
from social_automation.webhooks import extract_comment_events


CAMPAIGNS = {
    "campaigns": [
        {
            "campaign_id": "label",
            "platforms": ["instagram", "facebook"],
            "media_ids": {"instagram": ["ig-media"], "facebook": ["fb-post"]},
            "keywords": ["LABEL"],
            "public_reply": "Sent!",
            "private_message": "Guide: {asset_url}",
            "asset_url": "https://example.com/guide.pdf",
        }
    ]
}


class FakeDelivery:
    def __init__(self):
        self.private = []
        self.public = []

    def private_reply(self, platform, comment_id, message):
        self.private.append((platform, comment_id, message))
        return {"message_id": "private-1"}

    def public_reply(self, platform, comment_id, message):
        self.public.append((platform, comment_id, message))
        return {"id": "public-1"}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "campaigns.json"
        path.write_text(json.dumps(CAMPAIGNS), encoding="utf-8")
        self.registry = CampaignRegistry(path)
        self.store = MemoryDeliveryStore()
        self.delivery = FakeDelivery()

    def tearDown(self):
        self.temp.cleanup()

    def test_extracts_instagram_and_facebook_comments(self):
        instagram = {"object": "instagram", "entry": [{"changes": [{"field": "comments", "value": {"id": "ig-c", "text": "LABEL", "media": {"id": "ig-media"}, "from": {"id": "person"}}}]}]}
        facebook = {"object": "page", "entry": [{"changes": [{"field": "feed", "value": {"item": "comment", "verb": "add", "comment_id": "fb-c", "message": "label please", "post_id": "fb-post", "from": {"id": "person"}}}]}]}
        self.assertEqual(extract_comment_events(instagram)[0].platform, "instagram")
        self.assertEqual(extract_comment_events(facebook)[0].platform, "facebook")

    def test_signed_webhook_delivers_once(self):
        payload = {"object": "instagram", "entry": [{"changes": [{"field": "comments", "value": {"id": "ig-c", "text": "LABEL please", "media": {"id": "ig-media"}, "from": {"id": "person", "username": "Ana"}}}]}]}
        body = json.dumps(payload, separators=(",", ":")).encode()
        secret = "test-secret"
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"META_APP_SECRET": secret, "INSTAGRAM_USER_ID": "account"}, clear=False):
            app = create_app(registry=self.registry, store=self.store, delivery=self.delivery)
            client = app.test_client()
            first = client.post("/webhook", data=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
            second = client.post("/webhook", data=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["events"][0]["status"], "delivered")
        self.assertEqual(second.json["events"][0]["status"], "duplicate")
        self.assertEqual(len(self.delivery.private), 1)
        self.assertIn("guide.pdf", self.delivery.private[0][2])

    def test_rejects_invalid_signature(self):
        with patch.dict(os.environ, {"META_APP_SECRET": "secret"}, clear=False):
            app = create_app(registry=self.registry, store=self.store, delivery=self.delivery)
            response = app.test_client().post("/webhook", json={"object": "instagram"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
