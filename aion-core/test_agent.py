import json
import os
import tempfile
import unittest

import agent
from agent import run_agent_turn
from config import CONFIG


class TestAgent(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_workspace = CONFIG.get("agent_workspace_root")
        self.old_enabled = CONFIG.get("agent_enabled", True)
        self.old_confirm = CONFIG.get("agent_confirm_writes", True)
        CONFIG["agent_workspace_root"] = self.temp_dir.name
        CONFIG["agent_enabled"] = True
        CONFIG["agent_confirm_writes"] = True
        agent._PENDING.clear()

    def tearDown(self):
        CONFIG["agent_workspace_root"] = self.old_workspace
        CONFIG["agent_enabled"] = self.old_enabled
        CONFIG["agent_confirm_writes"] = self.old_confirm
        agent._PENDING.clear()
        self.temp_dir.cleanup()

    def _planner(self, payload):
        return json.dumps(payload)

    def test_regular_chat_is_not_handled(self):
        result = run_agent_turn(
            "tell me a joke",
            username="brian",
            session_id="global:lobby",
            planner=lambda messages: self._planner({"mode": "agent", "summary": "unused", "actions": []}),
        )

        self.assertIsNone(result)

    def test_read_action_executes_without_confirmation(self):
        path = os.path.join(self.temp_dir.name, "sample.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("needle in a haystack")

        result = run_agent_turn(
            "inspect the repo for needle",
            username="brian",
            session_id="global:lobby",
            planner=lambda messages: self._planner(
                {
                    "mode": "agent",
                    "summary": "I checked the repo.",
                    "actions": [{"tool": "repo.search", "args": {"query": "needle"}}],
                }
            ),
        )

        self.assertIsNotNone(result)
        self.assertIn("I checked the repo.", result.response)
        self.assertIn("sample.txt", result.response)
        self.assertFalse(result.staged)

    def test_repo_write_stages_until_yes_then_applies(self):
        path = os.path.join(self.temp_dir.name, "app.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("hello old world")

        first = run_agent_turn(
            "fix app.txt",
            username="brian",
            session_id="global:lobby",
            planner=lambda messages: self._planner(
                {
                    "mode": "agent",
                    "summary": "I can patch that.",
                    "actions": [
                        {
                            "tool": "repo.replace_text",
                            "args": {"path": "app.txt", "old": "old", "new": "new"},
                        }
                    ],
                }
            ),
        )

        self.assertTrue(first.staged)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello old world")

        second = run_agent_turn(
            "yes",
            username="brian",
            session_id="global:lobby",
            planner=lambda messages: "{}",
        )

        self.assertIn("Done. I ran the staged action.", second.response)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello new world")

    def test_cancel_drops_pending_action(self):
        path = os.path.join(self.temp_dir.name, "app.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("old")

        run_agent_turn(
            "fix app.txt",
            username="brian",
            session_id="global:lobby",
            planner=lambda messages: self._planner(
                {
                    "mode": "agent",
                    "summary": "Patch it.",
                    "actions": [{"tool": "repo.replace_text", "args": {"path": "app.txt", "old": "old", "new": "new"}}],
                }
            ),
        )
        result = run_agent_turn("cancel", username="brian", session_id="global:lobby", planner=lambda messages: "{}")

        self.assertIn("Cancelled", result.response)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old")

    def test_dangerous_action_is_blocked(self):
        result = run_agent_turn(
            "delete everything",
            username="brian",
            session_id="global:lobby",
            planner=lambda messages: self._planner(
                {
                    "mode": "agent",
                    "summary": "Nuke it.",
                    "actions": [{"tool": "shell.exec", "args": {"cmd": "rm -rf /"}}],
                }
            ),
        )

        self.assertIn("I’m not running that", result.response)
        self.assertIn("paperweights", result.response)


if __name__ == "__main__":
    unittest.main()
