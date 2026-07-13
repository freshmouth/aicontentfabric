from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from social_scheduler.meta_graph import MetaGraphError
from social_scheduler.scheduler import publish_record


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


if __name__ == "__main__":
    unittest.main()
