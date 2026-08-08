from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from factory_dashboard.app import main
from factory_dashboard.app.services.accounts import AccountCatalog
from factory_dashboard.app.services.attachments import AttachmentStorage
from factory_dashboard.app.services.openai_creative import OpenAICreativeService, validate_source_config
from factory_dashboard.app.services.creative_contract import CreativeContractError, compile_source_config
from factory_dashboard.app.store import LocalJsonStore
from tools.account_autopilot import (
    AccountAutopilotError,
    build_manual_execution_config,
    derive_visual_hook_text,
    first_frame_passed,
    load_generation_request,
    run_autopilot,
)


class FakeCreative:
    def __init__(self):
        self.last_attachments = []

    def create_or_revise(self, *, account_context, message, current_draft=None, attachments=None):
        self.last_attachments = attachments or []
        source = json.loads(json.dumps(account_context["source_config"]))
        source["concept_id"] = "dashboard_test_concept"
        return {
            "assistant_message": "Built a stricter hook and preserved the account identity.",
            "suggested_actions": ["Show me the full script", "Make the hook sharper"],
            "title": "Dashboard test concept",
            "brief": message,
            "caption": "A contextual caption. Comment SAL for the source.",
            "source_config": source,
        }


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "queued"}

    def find_run(self, request_id):
        return None


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LocalJsonStore(Path(self.temp.name) / "dashboard.json")
        main.store = self.store
        main.catalog = AccountCatalog(main.settings, self.store)
        main.creative = FakeCreative()
        main.github = FakeGitHub()
        attachment_settings = replace(main.settings, root=Path(self.temp.name), upload_bucket="")
        main.attachment_storage = AttachmentStorage(attachment_settings)
        self.client = TestClient(main.app)

    def tearDown(self):
        self.temp.cleanup()

    def test_bootstrap_returns_isolated_account_state(self):
        response = self.client.get("/api/bootstrap?account_id=sal_celtica")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["selected_account_id"], "sal_celtica")
        self.assertTrue(any(account["account_id"] == "sal_celtica" for account in payload["accounts"]))
        self.assertEqual(payload["drafts"], [])

    def test_chat_draft_dispatches_account_scoped_payload(self):
        response = self.client.post(
            "/api/chat",
            json={"account_id": "sal_celtica", "message": "Create a sharper culinary comparison."},
        )
        self.assertEqual(response.status_code, 200)
        draft = response.json()["draft"]
        self.assertEqual(draft["creative_spec"]["account_id"], "sal_celtica")

        queued = self.client.post(
            f"/api/drafts/{draft['id']}/generate",
            json={"dry_run": True, "skip_publish": True},
        )
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(len(main.github.calls), 1)
        call = main.github.calls[0]
        self.assertEqual(call["account_id"], "sal_celtica")
        self.assertEqual(call["payload"]["source_config"]["account_id"], "sal_celtica")

    def test_agent_chat_can_queue_omni_generation(self):
        response = self.client.post(
            "/api/chat",
            json={
                "account_id": "sal_celtica",
                "message": "Generate the video with Omni, captions, visual hook, music, and assembly.",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNotNone(payload["job"])
        self.assertEqual(payload["execution"]["action"], "generate_only")
        self.assertEqual(payload["execution"]["status"], "queued")
        self.assertTrue(payload["job"]["skip_publish"])
        self.assertFalse(main.github.calls[-1]["dry_run"])
        self.assertTrue(main.github.calls[-1]["skip_publish"])

    def test_agent_chat_can_queue_metricool_publish(self):
        response = self.client.post(
            "/api/chat",
            json={
                "account_id": "sal_celtica",
                "message": "Generate this reel and publish it through Metricool now.",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["execution"]["action"], "generate_and_publish")
        self.assertEqual(payload["execution"]["status"], "queued")
        self.assertFalse(payload["job"]["skip_publish"])
        self.assertFalse(main.github.calls[-1]["dry_run"])
        self.assertFalse(main.github.calls[-1]["skip_publish"])

    def test_agent_chat_respects_a_list_of_negated_actions(self):
        response = self.client.post(
            "/api/chat",
            json={
                "account_id": "speliers",
                "message": "Show me the concept, but do not generate, render, run, publish, or post anything.",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["execution"]["action"], "none")
        self.assertIsNone(payload["job"])
        self.assertEqual(main.github.calls, [])

    def test_agent_chat_can_generate_without_publishing(self):
        response = self.client.post(
            "/api/chat",
            json={
                "account_id": "sal_celtica",
                "message": "Generate the final reel, but don't publish or post it.",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["execution"]["action"], "generate_only")
        self.assertTrue(payload["job"]["skip_publish"])

    def test_streaming_chat_returns_progress_and_agent_actions(self):
        response = self.client.post(
            "/api/chat/stream",
            json={"account_id": "sal_celtica", "message": "Show me a stronger opening."},
        )

        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.splitlines() if line]
        self.assertEqual(events[0]["type"], "status")
        self.assertEqual(events[-1]["type"], "result")
        assistant = events[-1]["data"]["draft"]["chat_history"][-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["actions"], ["Show me the full script", "Make the hook sharper"])

    def test_creative_prompt_includes_prior_conversation(self):
        prompt = OpenAICreativeService._prompt(
            {
                "account": {
                    "account_id": "speliers",
                    "display_name": "Speliers",
                    "description": "Myth and cinema analysis.",
                },
                "source_config": {"language": "en"},
            },
            "Show me the revised ending.",
            {
                "creative_spec": {"concept_id": "cyclops_myth"},
                "chat_history": [
                    {"role": "user", "content": "Make the Cyclops hook personal."},
                    {"role": "assistant", "content": "I reframed the cave as avoidance."},
                ],
            },
            [],
        )

        self.assertIn("Make the Cyclops hook personal.", prompt)
        self.assertIn("I reframed the cave as avoidance.", prompt)
        self.assertIn("Show me the revised ending.", prompt)

    def test_request_loader_rejects_cross_account_source(self):
        path = Path(self.temp.name) / "request.json"
        path.write_text(
            json.dumps(
                {
                    "account_id": "sal_celtica",
                    "concept_id": "bad",
                    "source_config": {
                        "account_id": "hyperdash",
                        "hooks": [{}],
                        "mains": [{}],
                        "ctas": [{}],
                    },
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(AccountAutopilotError):
            load_generation_request(str(path), expected_account_id="sal_celtica")

    def test_photo_upload_is_sent_to_creative_draft(self):
        upload = self.client.post(
            "/api/accounts/sal_celtica/attachments",
            files={"file": ("salt-reference.png", b"\x89PNG\r\n\x1a\nreference", "image/png")},
        )
        self.assertEqual(upload.status_code, 200)
        attachment = upload.json()

        response = self.client.post(
            "/api/chat",
            json={
                "account_id": "sal_celtica",
                "message": "Use this photo as the visual source for a new creative.",
                "attachment_ids": [attachment["id"]],
            },
        )
        self.assertEqual(response.status_code, 200)
        draft = response.json()["draft"]
        self.assertEqual(draft["attachments"][0]["id"], attachment["id"])
        self.assertEqual(main.creative.last_attachments[0]["filename"], "salt-reference.png")
        self.assertTrue(main.creative.last_attachments[0]["data_url"].startswith("data:image/png;base64,"))

    def test_photo_cannot_cross_account_boundary(self):
        upload = self.client.post(
            "/api/accounts/sal_celtica/attachments",
            files={"file": ("salt-reference.png", b"\x89PNG\r\n\x1a\nreference", "image/png")},
        )
        attachment_id = upload.json()["id"]
        response = self.client.post(
            "/api/chat",
            json={
                "account_id": "hyperdash",
                "message": "Use the other account photo.",
                "attachment_ids": [attachment_id],
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("different account", response.json()["detail"])

    def test_manual_account_drafts_and_dispatches_without_autopilot(self):
        response = self.client.post(
            "/api/chat",
            json={
                "account_id": "beyond_the_label",
                "message": "Create a direct manual draft about a surprising grocery label.",
            },
        )
        self.assertEqual(response.status_code, 200)
        draft = response.json()["draft"]
        self.assertEqual(draft["account_id"], "beyond_the_label")
        self.assertEqual(draft["creative_spec"]["account_id"], "beyond_the_label")

        queued = self.client.post(
            f"/api/drafts/{draft['id']}/generate",
            json={"dry_run": True, "skip_publish": False},
        )
        self.assertEqual(queued.status_code, 200)
        call = main.github.calls[-1]
        self.assertEqual(call["account_id"], "beyond_the_label")
        self.assertEqual(call["payload"]["mode"], "manual_dashboard")

    def test_worker_manual_request_does_not_require_autopilot_config(self):
        account_dir = Path(self.temp.name) / "accounts" / "manual_brand"
        account_dir.mkdir(parents=True)
        config = build_manual_execution_config("manual_brand", account_dir)
        source = {
            "account_id": "manual_brand",
            "concept_id": "manual_test",
            "master_prompt": "Keep this account isolated.",
            "hooks": [{"id": "h", "script": "Hook", "prompt": "Hook"}],
            "mains": [{"id": "m", "segments": [{"id": "m1", "script": "Main", "prompt": "Main"}]}],
            "ctas": [{"id": "c", "script": "CTA", "prompt": "CTA"}],
        }
        request = {
            "account_id": "manual_brand",
            "concept_id": "manual_test",
            "caption": "Manual caption",
            "source_config": source,
            "reference_attachments": [],
        }
        args = SimpleNamespace(
            request_file="",
            today="2026-08-04",
            concept_id="",
            publish_at="",
            plan_only=True,
            force=True,
            skip_publish=False,
            dry_run=True,
            request_id="manual_request",
        )
        result = run_autopilot(
            "manual_brand",
            account_dir,
            account_dir / "autopilot_v3.json",
            config,
            args,
            generation_request=request,
            account_config={"account_id": "manual_brand", "display_name": "Manual Brand"},
        )
        self.assertEqual(result["execution_mode"], "manual_dashboard")
        self.assertEqual(result["concept_id"], "manual_test")
        self.assertTrue(result["plan_only"])

    def test_creative_scene_count_has_no_upper_limit(self):
        spec = {
            "concept_id": "scene_overflow",
            "account_id": "sal_celtica",
            "master_prompt": "Keep the account identity consistent.",
            "hooks": [
                {"id": "h1", "script": "First hook.", "prompt": "First hook visual."},
                {"id": "h2", "script": "Second hook.", "prompt": "Second hook visual."},
            ],
            "mains": [
                {
                    "id": "main",
                    "segments": [
                        {
                            "id": f"scene_{index:02d}",
                            "script": f"Main line {index}.",
                            "prompt": f"Main visual {index}.",
                        }
                        for index in range(1, 16)
                    ],
                }
            ],
            "ctas": [
                {"id": "c1", "script": "First CTA.", "prompt": "First CTA visual."},
                {"id": "c2", "script": "Second CTA.", "prompt": "Second CTA visual."},
            ],
        }

        validate_source_config(spec)

        self.assertEqual(len(spec["hooks"]), 2)
        self.assertEqual(len(spec["ctas"]), 2)
        self.assertEqual(len(spec["mains"][0]["segments"]), 15)

    def test_creative_preflight_expands_two_second_scene_before_generation(self):
        spec = {
            "concept_id": "duration_guard",
            "account_id": "claire",
            "master_prompt": "Keep Claire consistent.",
            "hooks": [
                {
                    "id": "hook",
                    "duration_seconds": 2,
                    "script": "Stop drinking plain water.",
                    "prompt": "Claire holds up a glass of water.",
                }
            ],
            "mains": [
                {
                    "id": "main",
                    "segments": [
                        {
                            "id": "main_01",
                            "duration_seconds": 5,
                            "script": "Here is what most people miss.",
                            "prompt": "Claire points to the glass.",
                        }
                    ],
                }
            ],
            "ctas": [
                {
                    "id": "cta",
                    "duration_seconds": 5,
                    "script": "Comment WATER for the product.",
                    "prompt": "Claire gestures toward the comments.",
                }
            ],
        }

        compiled, report = compile_source_config(spec)

        self.assertEqual(compiled["hooks"][0]["duration_seconds"], 3)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["adjusted_scene_count"], 1)

    def test_creative_preflight_rejects_dialogue_that_cannot_finish(self):
        long_line = " ".join(["word"] * 30)
        spec = {
            "concept_id": "too_long",
            "account_id": "claire",
            "master_prompt": "Keep Claire consistent.",
            "hooks": [{"id": "hook", "script": long_line, "prompt": "Hook visual."}],
            "mains": [{"id": "main", "script": "Short main.", "prompt": "Main visual."}],
            "ctas": [{"id": "cta", "script": "Short CTA.", "prompt": "CTA visual."}],
        }

        with self.assertRaisesRegex(CreativeContractError, "Split its dialogue"):
            compile_source_config(spec)

    def test_visual_hook_prefers_explicit_direct_overlay(self):
        text = derive_visual_hook_text(
            {
                "hooks": [
                    {
                        "hook_text": "This is the new Kinect for iPhone",
                        "script": "A longer ambiguous spoken opening that should not become the overlay.",
                    }
                ]
            }
        )
        self.assertEqual(text, "This is the new Kinect for iPhone")

    def test_first_frame_gate_requires_all_scores_and_no_issues(self):
        passing = {
            "status": "PASS",
            "product_placement": 9,
            "hand_realism": 8,
            "face_quality": 9,
            "background_realism": 8,
            "ugc_authenticity": 9,
            "issues": [],
        }
        self.assertTrue(first_frame_passed(passing))
        self.assertFalse(first_frame_passed({**passing, "hand_realism": 7}))
        self.assertFalse(first_frame_passed({**passing, "issues": ["floating object"]}))


if __name__ == "__main__":
    unittest.main()
