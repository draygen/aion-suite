import unittest
from unittest.mock import patch

import aion_api
from repair import RepairOutcome
from tools import ToolExecution, ToolRuntimeError


class AionApiTests(unittest.TestCase):
    @patch(
        "aion_api._ollama_runtime_status",
        return_value={"reachable": True, "model_loaded": False},
    )
    def test_health_exposes_aion_runtime_and_capabilities(self, mock_status):
        result = aion_api.health()

        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"], "ollama")
        self.assertIn("model", result)
        self.assertTrue(result["ollama"])
        self.assertFalse(result["model_loaded"])
        self.assertTrue(result["capabilities"]["streaming"])
        self.assertTrue(result["capabilities"]["safe_diagnostics"])
        self.assertTrue(result["capabilities"]["repair_proposals"])

    @patch("aion_api.handle_repair_command", return_value=None)
    @patch(
        "aion_api.dispatch_safe_diagnostic_message",
        return_value=ToolExecution(
            tool_id="ping",
            label="Ping",
            args={"target": "192.168.0.1"},
            output="PING result",
        ),
    )
    def test_action_returns_safe_diagnostic_result(self, mock_dispatch, mock_repair):
        result = aion_api.action(
            aion_api.ActionIn(message="ping 192.168.0.1", session_id="desktop:test")
        )

        self.assertTrue(result["handled"])
        self.assertEqual(result["kind"], "diagnostic")
        self.assertEqual(result["reply"], "PING result")
        self.assertFalse(result["requires_confirmation"])

    @patch(
        "aion_api.handle_repair_command",
        return_value=RepairOutcome(
            response="Proposed repair. Approve with token.",
            events=[],
            staged=True,
        ),
    )
    def test_action_preserves_repair_confirmation_state(self, mock_repair):
        result = aion_api.action(
            aion_api.ActionIn(
                message="repair diagnose mixed address family",
                session_id="desktop:test",
            )
        )

        self.assertTrue(result["handled"])
        self.assertEqual(result["kind"], "repair")
        self.assertTrue(result["requires_confirmation"])

    @patch("aion_api.handle_repair_command", return_value=None)
    @patch("aion_api.dispatch_safe_diagnostic_message", return_value=None)
    def test_action_leaves_normal_chat_unhandled(self, mock_dispatch, mock_repair):
        result = aion_api.action(
            aion_api.ActionIn(message="Explain mutex contention", session_id="desktop:test")
        )

        self.assertFalse(result["handled"])
        self.assertIsNone(result["reply"])

    @patch("aion_api.handle_repair_command", return_value=None)
    @patch("aion_api.propose_repair_for_failure")
    @patch(
        "aion_api.dispatch_safe_diagnostic_message",
        side_effect=ToolRuntimeError(
            "nmap_ping_sweep",
            "Nmap Ping Sweep",
            TypeError("mixed address family"),
        ),
    )
    def test_action_turns_recognized_tool_failure_into_proposal(
        self,
        mock_dispatch,
        mock_propose,
        mock_repair,
    ):
        mock_propose.return_value = RepairOutcome(
            response="Repair proposal",
            events=[],
            staged=True,
        )

        result = aion_api.action(
            aion_api.ActionIn(
                message="discover hosts 192.168.0.0/24",
                session_id="desktop:test",
            )
        )

        self.assertEqual(result["kind"], "repair")
        self.assertTrue(result["requires_confirmation"])
        mock_propose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
