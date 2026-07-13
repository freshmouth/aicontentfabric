from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from daily_factory.worker import build_omni_config, select_concept


ROOT = Path(__file__).resolve().parents[2]


class DailyWorkerTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "daily_factory" / "config.json").read_text(encoding="utf-8"))
        self.queue = json.loads((ROOT / "daily_factory" / "content_queue.json").read_text(encoding="utf-8"))

    def test_selection_is_deterministic(self):
        self.assertEqual(select_concept(self.queue, "2026-07-12"), select_concept(self.queue, "2026-07-12"))

    def test_generated_config_is_one_complete_variant(self):
        concept = select_concept(self.queue, "2026-07-12")
        with tempfile.TemporaryDirectory() as directory:
            generated = build_omni_config(self.config, concept, "2026-07-12", Path(directory))
        self.assertEqual(len(generated["hooks"]), 1)
        self.assertEqual(len(generated["meals"][0]["segments"]), 4)
        self.assertEqual(len(generated["ctas"]), 1)
        self.assertEqual(generated["variants"]["count"], 1)
        self.assertIn("master_reference.png", generated["hooks"][0]["reference_images"][0])


if __name__ == "__main__":
    unittest.main()
