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

    def test_can_select_structured_oatmeal_concept(self):
        concept = select_concept(self.queue, "2026-07-13", "oatmeal_sovereign_label_scan")
        self.assertEqual(concept["cta_keyword"], "LABEL")
        with tempfile.TemporaryDirectory() as directory:
            generated = build_omni_config(self.config, concept, "2026-07-13", Path(directory))
        self.assertEqual(len(generated["meals"][0]["segments"]), 5)
        self.assertEqual(generated["ctas"][0]["title"], "Comment LABEL")
        prompt_blob = json.dumps(generated)
        self.assertNotIn("brand-free", prompt_blob)
        self.assertNotIn("brand free", prompt_blob.lower())
        self.assertNotIn("Claire oats", prompt_blob)

    def test_generated_config_is_one_complete_variant(self):
        concept = select_concept(self.queue, "2026-07-12")
        with tempfile.TemporaryDirectory() as directory:
            generated = build_omni_config(self.config, concept, "2026-07-12", Path(directory))
        self.assertEqual(len(generated["hooks"]), 1)
        self.assertEqual(len(generated["meals"][0]["segments"]), 4)
        self.assertEqual(len(generated["ctas"]), 1)
        self.assertEqual(generated["variants"]["count"], 1)
        self.assertIn("master_reference.png", generated["hooks"][0]["reference_images"][0])

    def test_sarah_cole_uses_independent_character_reference(self):
        concept = select_concept(self.queue, "2026-07-13", "oatmeal_sovereign_label_scan")
        concept["character_id"] = "sarah_cole"
        with tempfile.TemporaryDirectory() as directory:
            generated = build_omni_config(self.config, concept, "2026-07-13", Path(directory))
        prompt_blob = json.dumps(generated)
        self.assertEqual(generated["character"]["character_id"], "sarah_cole")
        self.assertEqual(generated["hooks"][0]["reference_images"], ["characters/sarah_cole/master_reference.png"])
        self.assertIn("Sarah Cole", prompt_blob)
        self.assertNotIn("characters/claire_natural/master_reference.png", prompt_blob)
        self.assertNotIn("Claire holds", prompt_blob)


if __name__ == "__main__":
    unittest.main()
