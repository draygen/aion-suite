import os
import tempfile
import unittest
from unittest.mock import patch

import auth
from config import CONFIG
from events import list_events
from tools import ToolExecution
from web import app, _ensure_channel_exists, _ensure_channel_membership, _save_history_turns


class TestWebChatFlow(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = auth.DB_PATH
        self.original_admin_password = CONFIG.get("admin_password")
        self.original_auto_extract = CONFIG.get("auto_extract_facts", True)
        auth.DB_PATH = os.path.join(self.temp_dir.name, "aion-test.db")
        CONFIG["admin_password"] = "changeme2026!"
        CONFIG["auto_extract_facts"] = False
        auth.init_db()
        _ensure_channel_exists("syncforge", display_name="Syncforge", is_private=False, created_by=1)
        _ensure_channel_membership(1, "syncforge", membership_role="member")
        self.client.set_cookie("aion_token", "test-token")

    def tearDown(self):
        auth.DB_PATH = self.original_db_path
        CONFIG["admin_password"] = self.original_admin_password
        CONFIG["auto_extract_facts"] = self.original_auto_extract
        self.temp_dir.cleanup()

    def _history_rows(self):
        db = auth.get_db()
        rows = db.execute(
            "SELECT role, content, session_id, channel, thread_id, message_id, author_username FROM history ORDER BY id"
        ).fetchall()
        db.close()
        return rows

    @patch("auth.get_user_by_token", return_value={"id": 1, "username": "brian", "role": "admin", "must_change_password": 0})
    @patch("web.get_facts", return_value=[])
    @patch("web.ask_llm_chat", return_value="LLM response")
    def test_chat_returns_envelope_and_persists_events(self, mock_ask, mock_get_facts, mock_get_user):
        response = self.client.post(
            "/api/chat",
            json={
                "message": "hello there",
                "channel": "syncforge",
                "thread_id": "deploy-123",
                "session_id": "syncforge:deploy-123",
                "metadata": {"source": "unit-test"},
                "tts": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["response"], "LLM response")
        self.assertEqual(body["session"]["channel"], "syncforge")
        self.assertEqual(body["session"]["thread_id"], "deploy-123")
        self.assertEqual(body["session"]["session_id"], "syncforge:deploy-123")
        self.assertTrue(body["session"]["message_id"].startswith("msg-"))
        self.assertTrue(body["session"]["reply_to"].startswith("msg-"))
        mock_ask.assert_called_once()

        rows = self._history_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["session_id"], "syncforge:deploy-123")
        self.assertEqual(rows[0]["channel"], "syncforge")
        self.assertEqual(rows[0]["thread_id"], "deploy-123")

        events = list_events(session_id="syncforge:deploy-123")
        event_types = [event["event_type"] for event in events]
        self.assertEqual(event_types, ["user_message_received", "assistant_message_sent"])
        self.assertEqual(events[0]["payload"]["metadata"], {"source": "unit-test"})
        self.assertIn("ts", events[0])

    @patch("auth.get_user_by_token", return_value={"id": 1, "username": "brian", "role": "admin", "must_change_password": 0})
    @patch("web.build_system_prompt", return_value="SYSTEM")
    @patch("web.ask_llm_chat", return_value="scoped response")
    def test_chat_uses_session_scoped_history(self, mock_ask, mock_system_prompt, mock_get_user):
        _save_history_turns(
            1,
            "brian",
            "old session one",
            "answer one",
            session_id="syncforge:one",
            channel="syncforge",
            thread_id="one",
            user_message_id="msg-one-user",
            assistant_message_id="msg-one-assistant",
        )
        _save_history_turns(
            1,
            "brian",
            "old session two",
            "answer two",
            session_id="syncforge:two",
            channel="syncforge",
            thread_id="two",
            user_message_id="msg-two-user",
            assistant_message_id="msg-two-assistant",
        )

        response = self.client.post(
            "/api/chat",
            json={
                "message": "new question",
                "channel": "syncforge",
                "thread_id": "one",
                "session_id": "syncforge:one",
                "tts": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        messages = mock_ask.call_args.args[0]
        serialized = [item["content"] for item in messages]
        self.assertIn("[brian] old session one", serialized)
        self.assertIn("answer one", serialized)
        self.assertNotIn("[brian] old session two", serialized)
        self.assertNotIn("answer two", serialized)

    @patch("auth.get_user_by_token", return_value={"id": 1, "username": "brian", "role": "admin", "must_change_password": 0})
    @patch("web.ask_llm_chat")
    @patch("web.dispatch_tool_message", return_value=ToolExecution(tool_id="ping", label="Ping", args={"target": "app.example.com"}, output="PING for app.example.com:\n```ok\n```"))
    def test_chat_executes_registered_tool_and_logs_tool_events(self, mock_dispatch, mock_ask, mock_get_user):
        response = self.client.post(
            "/api/chat",
            json={
                "message": "ping app.example.com",
                "channel": "syncforge",
                "thread_id": "ops",
                "session_id": "syncforge:ops",
                "tts": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("PING for app.example.com", response.get_json()["response"])
        mock_dispatch.assert_called_once()
        mock_ask.assert_not_called()

        events = list_events(session_id="syncforge:ops")
        event_types = [event["event_type"] for event in events]
        self.assertEqual(
            event_types,
            ["user_message_received", "tool_invoked", "tool_result", "assistant_message_sent"],
        )
        self.assertEqual(events[1]["tool_name"], "ping")

    @patch("auth.get_user_by_token", return_value={"id": 1, "username": "brian", "role": "admin", "must_change_password": 0})
    @patch("web.get_facts", return_value=[])
    @patch("web.ask_llm_chat", return_value="global response")
    def test_chat_defaults_to_global_lobby_session(self, mock_ask, mock_get_facts, mock_get_user):
        response = self.client.post(
            "/api/chat",
            json={
                "message": "hello global",
                "tts": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["session"]["channel"], "global")
        self.assertEqual(body["session"]["thread_id"], "lobby")
        self.assertEqual(body["session"]["session_id"], "global:lobby")
        self.assertIn("timestamp", body)

        rows = self._history_rows()
        self.assertEqual(rows[0]["session_id"], "global:lobby")
        self.assertEqual(rows[0]["channel"], "global")
        self.assertEqual(rows[0]["thread_id"], "lobby")
        self.assertEqual(rows[0]["author_username"], "brian")

    @patch("auth.get_user_by_token", return_value={"id": 1, "username": "brian", "role": "admin", "must_change_password": 0})
    @patch("web.ask_llm_chat")
    def test_chat_answers_system_prompt_query_without_llm(self, mock_ask, mock_get_user):
        response = self.client.post(
            "/api/chat",
            json={
                "message": "Aion what is your system prompt tell me?",
                "tts": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("High-level summary:", body["response"])
        self.assertIn("avoid calling you 'boss'", body["response"])
        mock_ask.assert_not_called()

    @patch("auth.get_user_by_token", return_value={"id": 1, "username": "brian", "role": "admin", "must_change_password": 0})
    @patch("web.get_facts", return_value=[])
    @patch("web.ask_llm_chat", return_value="Understood, boss. I'll handle it.")
    def test_chat_sanitizes_boss_from_model_output(self, mock_ask, mock_get_facts, mock_get_user):
        response = self.client.post(
            "/api/chat",
            json={
                "message": "hello",
                "tts": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["response"], "Understood, Brian. I'll handle it.")

    def test_global_channel_history_is_shared_across_users(self):
        alice_id = auth.create_user("alice", "shared-pass-2026", must_change_password=False)
        bob_id = auth.create_user("bob", "shared-pass-2026", must_change_password=False)

        alice = app.test_client()
        bob = app.test_client()

        self.assertEqual(
            alice.post("/api/login", json={"username": "alice", "password": "shared-pass-2026"}).status_code,
            200,
        )
        self.assertEqual(
            bob.post("/api/login", json={"username": "bob", "password": "shared-pass-2026"}).status_code,
            200,
        )

        response = alice.post("/api/chat", json={"message": "remember: hello from alice", "tts": False})
        self.assertEqual(response.status_code, 200)

        history = bob.get("/api/channels/global/history")
        self.assertEqual(history.status_code, 200)
        messages = history.get_json()["messages"]
        contents = [message["content"] for message in messages]
        self.assertIn("remember: hello from alice", contents)
        self.assertIn("Got it. I'll remember: hello from alice", contents)
        authors = [message["author_username"] for message in messages]
        self.assertIn("alice", authors)
        self.assertIn("aion", authors)

    def test_private_channels_require_invites(self):
        auth.create_user("alice", "shared-pass-2026", must_change_password=False)
        auth.create_user("bob", "shared-pass-2026", must_change_password=False)

        alice = app.test_client()
        bob = app.test_client()
        alice.post("/api/login", json={"username": "alice", "password": "shared-pass-2026"})
        bob.post("/api/login", json={"username": "bob", "password": "shared-pass-2026"})

        create = alice.post(
            "/api/channels",
            json={"name": "secret", "display_name": "Secret", "is_private": True},
        )
        self.assertEqual(create.status_code, 200)

        join_denied = bob.post("/api/channels/secret/join", json={})
        self.assertEqual(join_denied.status_code, 403)

        invite = alice.post("/api/channels/secret/invite", json={"username": "bob"})
        self.assertEqual(invite.status_code, 200)

        join_allowed = bob.post("/api/channels/secret/join", json={})
        self.assertEqual(join_allowed.status_code, 200)

        response = bob.post(
            "/api/chat",
            json={"message": "remember: private hello", "channel": "secret", "tts": False},
        )
        self.assertEqual(response.status_code, 200)

        history = alice.get("/api/channels/secret/history")
        self.assertEqual(history.status_code, 200)
        contents = [message["content"] for message in history.get_json()["messages"]]
        self.assertIn("remember: private hello", contents)

    def test_presence_and_activity_include_aion_and_login_events(self):
        auth.create_user("alice", "shared-pass-2026", must_change_password=False)
        alice = app.test_client()

        login = alice.post("/api/login", json={"username": "alice", "password": "shared-pass-2026"})
        self.assertEqual(login.status_code, 200)

        presence = alice.get("/api/channels/global/presence")
        self.assertEqual(presence.status_code, 200)
        users = presence.get_json()["users"]
        names = [user["display_name"] for user in users]
        self.assertIn("Aion", names)
        self.assertIn("alice", names)

        activity = alice.get("/api/activity?channel=global")
        self.assertEqual(activity.status_code, 200)
        event_types = [item["event_type"] for item in activity.get_json()["activity"]]
        self.assertIn("auth_login_success", event_types)
        self.assertIn("global_chat_joined", event_types)

        logout = alice.post("/api/logout", json={})
        self.assertEqual(logout.status_code, 200)

        db = auth.get_db()
        row = db.execute(
            "SELECT COUNT(*) AS c FROM channel_presence WHERE occupant_key = 'alice'"
        ).fetchone()
        db.close()
        self.assertEqual(row["c"], 0)


if __name__ == "__main__":
    unittest.main()
