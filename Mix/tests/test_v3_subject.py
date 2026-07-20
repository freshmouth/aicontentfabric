from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from Mix.v3.image_prompting import build_image_prompt
from Mix.v3.image_qa import build_fixed_prompt, build_qa_prompt


ROOT = Path(__file__).resolve().parents[2]


class MixV3SubjectTests(unittest.TestCase):
    def test_default_subject_preserves_product_wording(self) -> None:
        scene = {"prompt": "A hand holds the item close to camera."}
        qa_prompt = build_qa_prompt(build_image_prompt(scene), scene=scene)
        self.assertIn("product_placement", qa_prompt)
        self.assertIn("the product is naturally held in hand or placed on surface", qa_prompt)

    def test_custom_subject_updates_prompt_qa_and_fix_text(self) -> None:
        project = {
            "subject_label": "kombucha scoby",
            "subject_placement_hint": "held gently in a jar or resting on a clean plate",
        }
        scene = {"prompt": "Kitchen counter, person shows the jar."}
        image_prompt = build_image_prompt(scene, project=project)
        qa_prompt = build_qa_prompt(image_prompt, project=project, scene=scene)
        fixed = build_fixed_prompt(
            image_prompt,
            {"product_placement": 4, "issues": ["floating subject"]},
            project=project,
            scene=scene,
        )
        self.assertIn("kombucha scoby", image_prompt)
        self.assertIn("product_placement", qa_prompt)
        self.assertIn("kombucha scoby is held gently in a jar", qa_prompt)
        self.assertIn("The kombucha scoby MUST be held gently in a jar", fixed)

    def test_pipeline_writes_execution_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v3"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "Mix" / "v3" / "pipeline_v3.py"),
                    "--config",
                    str(ROOT / "Mix" / "config.v3.dynamic_subject.example.json"),
                    "--out",
                    str(out),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((out / "v3_execution_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(manifest["subject_label"], "sal mineral")
            self.assertGreater(manifest["scene_count"], 0)


if __name__ == "__main__":
    unittest.main()
