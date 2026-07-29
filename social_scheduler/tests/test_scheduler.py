from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from social_scheduler.meta_graph import MetaGraphError, load_config
from social_scheduler.scheduler import SchedulerError, load_env, publish_record, validate_config_scope


class PartialFailureClient:
    def __init__(self, _config):
        pass

    def publish_instagram_reel(self, **_kwargs):
        return {"platform": "instagram", "post_id": "ig-post"}

    def publish_facebook_reel(self, **_kwargs):
        raise MetaGraphError("Facebook rejected the upload")


class SuccessfulFacebookClient:
    def __init__(self, _config):
        pass

    def publish_facebook_reel(self, **_kwargs):
        return {"platform": "facebook", "post_id": "fb-post"}


class SuccessfulMetricoolClient:
    def __init__(self, _config):
        pass

    def build_scheduled_post_payload(self, **kwargs):
        return {
            "publicationDate": {"dateTime": "2026-07-29T09:00:00", "timezone": "America/Mexico_City"},
            "text": kwargs["caption"],
            "providers": [{"network": item} for item in kwargs["platforms"]],
            "media": [kwargs["media_url"]],
        }

    def schedule_reel(self, **_kwargs):
        return {
            "provider": "metricool",
            "status": "scheduled",
            "metricool_post_id": 123,
            "metricool_post_uuid": "metricool-post-uuid",
        }


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.video = Path(self.temp.name) / "video.mp4"
        self.video.write_bytes(b"test-video")

    def tearDown(self):
        self.temp.cleanup()

    def test_partial_failure_retries_only_failed_platform(self):
        record = {
            "id": "test",
            "video": str(self.video),
            "public_video_url": "https://example.com/video.mp4",
            "caption": "Caption",
            "platforms": ["instagram", "facebook"],
            "attempts": [],
        }
        with patch("social_scheduler.scheduler.MetaGraphClient", PartialFailureClient):
            result = publish_record(record, config={"meta_graph": {}}, dry_run=False, skip_hosting=True)

        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual(record["platforms"], ["facebook"])
        self.assertEqual(result["attempt"]["successful_platforms"], ["instagram"])
        self.assertEqual(result["attempt"]["failed_platforms"], ["facebook"])

        with patch("social_scheduler.scheduler.MetaGraphClient", SuccessfulFacebookClient):
            retry = publish_record(record, config={"meta_graph": {}}, dry_run=False, skip_hosting=True)

        self.assertEqual(retry["status"], "published")
        self.assertEqual(retry["attempt"]["successful_platforms"], ["facebook"])
        self.assertNotIn("instagram", retry["attempt"]["platforms"])

    def test_account_config_rejects_generic_token_env_names(self):
        config = {
            "account_id": "hyperdash",
            "meta_graph": {
                "instagram_access_token_env": "INSTAGRAM_ACCESS_TOKEN",
                "facebook_access_token_env": "HYPERDASH_FACEBOOK_PAGE_ACCESS_TOKEN",
            },
        }

        with self.assertRaises(SchedulerError):
            validate_config_scope(config)

    def test_account_config_rejects_generic_metricool_env_names(self):
        config = {
            "account_id": "hyperdash",
            "publisher": "metricool",
            "metricool": {
                "user_id": "METRICOOL_USER_ID",
                "blog_id": "HYPERDASH_METRICOOL_BLOG_ID",
                "api_token_env": "HYPERDASH_METRICOOL_API_TOKEN",
            },
        }

        with self.assertRaises(SchedulerError):
            validate_config_scope(config)

    def test_meta_ids_can_resolve_from_env_names(self):
        config = {
            "meta_graph": {
                "instagram_user_id": "HYPERDASH_INSTAGRAM_USER_ID",
                "facebook_page_id": "HYPERDASH_FACEBOOK_PAGE_ID",
            }
        }

        with patch.dict(
            "os.environ",
            {
                "HYPERDASH_INSTAGRAM_USER_ID": "38400671449531960",
                "HYPERDASH_FACEBOOK_PAGE_ID": "1234567890",
            },
        ):
            loaded = load_config(config)

        self.assertEqual(loaded.instagram_user_id, "38400671449531960")
        self.assertEqual(loaded.facebook_page_id, "1234567890")

    def test_account_secrets_override_shared_env_values(self):
        shared = Path(self.temp.name) / ".env.local"
        account = Path(self.temp.name) / "secrets.env"
        shared.write_text("HYPERDASH_FACEBOOK_PAGE_ID=old-page\n", encoding="utf-8")
        account.write_text("HYPERDASH_FACEBOOK_PAGE_ID=1234567890\n", encoding="utf-8")

        with patch.dict("os.environ", {}, clear=True):
            load_env(shared)
            load_env(account, override=True)
            loaded = load_config({"meta_graph": {"facebook_page_id": "HYPERDASH_FACEBOOK_PAGE_ID"}})

        self.assertEqual(loaded.facebook_page_id, "1234567890")

    def test_env_loader_accepts_utf8_bom_files(self):
        account = Path(self.temp.name) / "secrets.env"
        account.write_text("\ufeffHYPERDASH_INSTAGRAM_USER_ID=38400671449531960\n", encoding="utf-8")

        with patch.dict("os.environ", {}, clear=True):
            load_env(account, override=True)
            loaded = load_config({"meta_graph": {"instagram_user_id": "HYPERDASH_INSTAGRAM_USER_ID"}})

        self.assertEqual(loaded.instagram_user_id, "38400671449531960")

    def test_publish_rejects_record_account_mismatch(self):
        record = {
            "id": "test",
            "account_id": "claire",
            "video": str(self.video),
            "public_video_url": "https://example.com/video.mp4",
            "caption": "Caption",
            "platforms": ["facebook"],
            "attempts": [],
        }
        config = {
            "account_id": "hyperdash",
            "meta_graph": {
                "facebook_access_token_env": "HYPERDASH_FACEBOOK_PAGE_ACCESS_TOKEN",
                "instagram_access_token_env": "HYPERDASH_INSTAGRAM_ACCESS_TOKEN",
            },
        }

        with self.assertRaises(SchedulerError):
            publish_record(record, config=config, dry_run=True, skip_hosting=True)

    def test_publish_rejects_manifest_account_mismatch(self):
        manifest = Path(self.temp.name) / "manifest.json"
        manifest.write_text('{"account_id": "beyond_the_label"}', encoding="utf-8")
        record = {
            "id": "test",
            "account_id": "hyperdash",
            "video": str(self.video),
            "manifest": str(manifest),
            "public_video_url": "https://example.com/video.mp4",
            "caption": "Caption",
            "platforms": ["facebook"],
            "attempts": [],
        }
        config = {
            "account_id": "hyperdash",
            "meta_graph": {
                "facebook_access_token_env": "HYPERDASH_FACEBOOK_PAGE_ACCESS_TOKEN",
                "instagram_access_token_env": "HYPERDASH_INSTAGRAM_ACCESS_TOKEN",
            },
        }

        with self.assertRaises(SchedulerError):
            publish_record(record, config=config, dry_run=True, skip_hosting=True)

    def test_metricool_dry_run_builds_account_scoped_payload(self):
        record = {
            "id": "test",
            "account_id": "hyperdash",
            "video": str(self.video),
            "caption": "Caption",
            "first_comment": "Comment RUN",
            "platforms": ["instagram", "facebook"],
            "publish_at": "2026-07-29T15:00:00Z",
            "attempts": [],
        }
        config = {
            "account_id": "hyperdash",
            "publisher": "metricool",
            "metricool": {
                "user_id": "HYPERDASH_METRICOOL_USER_ID",
                "blog_id": "HYPERDASH_METRICOOL_BLOG_ID",
                "api_token_env": "HYPERDASH_METRICOOL_API_TOKEN",
                "timezone": "America/Mexico_City",
            },
        }

        result = publish_record(record, config=config, dry_run=True, skip_hosting=True)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["attempt"]["publisher"], "metricool")
        payload = result["attempt"]["platforms"]["metricool"]["payload"]
        self.assertEqual(payload["text"], "Caption")
        self.assertEqual(payload["publicationDate"]["timezone"], "America/Mexico_City")
        self.assertEqual([item["network"] for item in payload["providers"]], ["instagram", "facebook"])

    def test_metricool_success_marks_record_scheduled(self):
        record = {
            "id": "test",
            "account_id": "hyperdash",
            "video": str(self.video),
            "caption": "Caption",
            "platforms": ["instagram"],
            "publish_at": "2026-07-29T15:00:00Z",
            "attempts": [],
        }
        config = {
            "account_id": "hyperdash",
            "publisher": "metricool",
            "metricool": {
                "user_id": "HYPERDASH_METRICOOL_USER_ID",
                "blog_id": "HYPERDASH_METRICOOL_BLOG_ID",
                "api_token_env": "HYPERDASH_METRICOOL_API_TOKEN",
            },
        }

        with patch("social_scheduler.scheduler.MetricoolClient", SuccessfulMetricoolClient):
            result = publish_record(record, config=config, dry_run=False, skip_hosting=True)

        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(record["platforms"], [])
        self.assertEqual(result["attempt"]["platforms"]["metricool"]["metricool_post_id"], 123)


if __name__ == "__main__":
    unittest.main()
