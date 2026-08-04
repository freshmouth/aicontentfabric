from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from factory_dashboard.app import main
from factory_dashboard.app.services.accounts import AccountCatalog
from factory_dashboard.app.store import LocalJsonStore
from tools.account_autopilot import AccountAutopilotError, load_generation_request


class FakeCreative:
    def create_or_revise(self, *, account_context, message, current_draft=None):
        source = json.loads(json.dumps(account_context["source_config"]))
        source["concept_id"] = "dashboard_test_concept"
        return {
            "assistant_message": "Built a stricter hook and preserved the account identity.",
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


if __name__ == "__main__":
    unittest.main()
