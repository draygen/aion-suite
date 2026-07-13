import time
import unittest
from unittest.mock import patch

import auth
from agent import AgentOutcome
from config import CONFIG
from events import list_events
from web import app


class TestWebAgentFlow(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.original_auto_extract = CONFIG.get("auto_extract_facts", True)
        self.original_agent_enabled = CONFIG.get("agent_enabled", True)
        CONFIG["auto_extract_facts"] = False
        CONFIG["agent_enabled"] = True
        self.username = f"agenttest{int(time.time() * 1000)}"
        self.user_id = auth.create_user(self.username, "agent-test-pass-2026", must_change_password=False)
        self.token = auth.create_token(self.user_id)
        self.client.set_cookie("aion_token", self.token)

    def tearDown(self):
        auth.delete_user(self.user_id)
        CONFIG["auto_extract_facts"] = self.original_auto_extract
        CONFIG["agent_enabled"] = self.original_agent_enabled

    @patch("web.ask_llm_chat")
    @patch("web.dispatch_tool_message", return_value=None)
    @patch("web.run_agent_turn")
    def test_agent_result_bypasses_llm_and_logs_events(self, mock_agent, mock_dispatch, mock_ask):
        mock_agent.return_value = AgentOutcome(
            response="I checked it. The file is boring, which is legally allowed.",
            events=[
                {"event_type": "agent_plan", "content": "Inspect repo", "payload": {"actions": []}},
                {"event_type": "agent_tool_result", "tool_name": "repo.search", "content": "match", "payload": {}},
            ],
        )
        session_id = f"global:agent-{self.username}"

        response = self.client.post(
            "/api/chat",
            json={
                "message": "inspect the repo for boring files",
                "channel": "global",
                "thread_id": f"agent-{self.username}",
                "session_id": session_id,
                "tts": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("I checked it", response.get_json()["response"])
        mock_agent.assert_called_once()
        mock_ask.assert_not_called()

        event_types = [event["event_type"] for event in list_events(session_id=session_id)]
        self.assertEqual(
            event_types,
            ["user_message_received", "agent_plan", "agent_tool_result", "assistant_message_sent"],
        )


if __name__ == "__main__":
    unittest.main()
