from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from .campaigns import CampaignRegistry
from .meta_delivery import MetaDeliveryClient
from .models import CommentEvent
from .store import FirestoreDeliveryStore, JsonDeliveryStore
from .webhooks import extract_comment_events


LOG = logging.getLogger("social_automation")


def create_app(*, registry=None, store=None, delivery=None) -> Flask:
    app = Flask(__name__)
    registry = registry or CampaignRegistry(Path(os.environ.get("CAMPAIGNS_FILE", "social_automation/campaigns.json")))
    store = store or _build_store()
    delivery = delivery or MetaDeliveryClient(
        graph_version=os.environ.get("META_GRAPH_VERSION", "v23.0"),
        instagram_user_id=os.environ.get("INSTAGRAM_USER_ID", ""),
        facebook_page_id=os.environ.get("FACEBOOK_PAGE_ID", ""),
    )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "meta-comment-automation"})

    @app.get("/webhook")
    def verify_webhook():
        mode = request.args.get("hub.mode", "")
        token = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")
        expected = os.environ.get("META_WEBHOOK_VERIFY_TOKEN", "").strip()
        if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
            return Response(challenge, status=200, mimetype="text/plain")
        return jsonify({"error": "webhook verification failed"}), 403

    @app.post("/webhook")
    def receive_webhook():
        raw_body = request.get_data(cache=True)
        if not _valid_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
            return jsonify({"error": "invalid signature"}), 401
        payload = request.get_json(silent=True) or {}
        results = [_handle_event(event, registry, store, delivery) for event in extract_comment_events(payload)]
        status_code = 503 if any(item.get("status") == "failed" for item in results) else 200
        return jsonify({"received": status_code == 200, "events": results}), status_code

    return app


def _build_store():
    default_backend = "firestore" if os.environ.get("K_SERVICE") else "json"
    backend = os.environ.get("DELIVERY_STORE", default_backend).strip().lower()
    if backend == "json":
        return JsonDeliveryStore(Path(os.environ.get("DELIVERY_LOG_PATH", "/tmp/meta_comment_deliveries.json")))
    return FirestoreDeliveryStore(os.environ.get("FIRESTORE_COLLECTION", "meta_comment_deliveries"))


def _valid_signature(body: bytes, supplied: str) -> bool:
    secret = os.environ.get("META_APP_SECRET", "").strip()
    if not secret:
        return os.environ.get("ALLOW_UNSIGNED_WEBHOOKS", "0") == "1"
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return bool(supplied) and hmac.compare_digest(expected, supplied)


def _handle_event(event: CommentEvent, registry, store, delivery) -> dict[str, Any]:
    own_ids = {os.environ.get("INSTAGRAM_USER_ID", ""), os.environ.get("FACEBOOK_PAGE_ID", "")}
    if event.author_id and event.author_id in own_ids:
        return {"event_key": event.event_key, "status": "ignored_self"}
    match = registry.match(event)
    if not match:
        return {"event_key": event.event_key, "status": "ignored_no_match"}
    campaign, keyword = match
    base = {
        "platform": event.platform,
        "comment_id": event.comment_id,
        "media_id": event.media_id,
        "campaign_id": campaign.campaign_id,
        "keyword": keyword,
    }
    if not store.claim(event.event_key, base):
        return {**base, "status": "duplicate"}
    try:
        private_response = delivery.private_reply(
            event.platform,
            event.comment_id,
            campaign.render_private_message(event, keyword),
        )
        try:
            public_response = delivery.public_reply(event.platform, event.comment_id, campaign.public_reply)
        except Exception as exc:
            # The requested asset was delivered; a public acknowledgement is optional.
            public_response = {"status": "failed_optional", "error": str(exc)}
        details = {**base, "private_response": private_response, "public_response": public_response}
        store.complete(event.event_key, details)
        return {**base, "status": "delivered"}
    except Exception as exc:
        LOG.exception("Delivery failed for %s", event.event_key)
        store.fail(event.event_key, str(exc), base)
        return {**base, "status": "failed", "error": str(exc)}


app = create_app()
