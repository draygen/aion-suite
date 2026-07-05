import unittest
from unittest.mock import MagicMock, patch

import vast


class TestVast(unittest.TestCase):
    def setUp(self):
        self._original_api_key = vast.CONFIG.get("vast_api_key")
        self._original_repo_url = vast.CONFIG.get("vast_repo_url")
        self._original_repo_branch = vast.CONFIG.get("vast_repo_branch")
        vast.CONFIG["vast_api_key"] = "test-key"
        vast.CONFIG["vast_repo_url"] = "https://github.com/draygen/drayhub-platform.git"
        vast.CONFIG["vast_repo_branch"] = "main"

    def tearDown(self):
        vast.CONFIG["vast_api_key"] = self._original_api_key
        vast.CONFIG["vast_repo_url"] = self._original_repo_url
        vast.CONFIG["vast_repo_branch"] = self._original_repo_branch

    @patch("vast.requests.get")
    def test_get_instance_extracts_nested_payload(self, mock_get):
        response = MagicMock()
        response.json.return_value = {"instances": [{"id": 123, "actual_status": "running"}]}
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        result = vast.get_instance(123)

        self.assertEqual(result["id"], 123)
        self.assertEqual(result["actual_status"], "running")

    @patch("vast.requests.put")
    def test_start_instance_sends_running_state(self, mock_put):
        response = MagicMock()
        response.json.return_value = {"success": True}
        response.raise_for_status.return_value = None
        mock_put.return_value = response

        result = vast.start_instance(42)

        self.assertEqual(result["success"], True)
        self.assertEqual(mock_put.call_args.kwargs["json"], {"state": "running"})

    @patch("vast.requests.put")
    def test_stop_instance_sends_stopped_state(self, mock_put):
        response = MagicMock()
        response.json.return_value = {"success": True}
        response.raise_for_status.return_value = None
        mock_put.return_value = response

        result = vast.stop_instance(42)

        self.assertEqual(result["success"], True)
        self.assertEqual(mock_put.call_args.kwargs["json"], {"state": "stopped"})

    @patch("vast.start_instance", return_value={"started": True})
    @patch("vast.stop_instance", return_value={"stopped": True})
    def test_restart_instance_chains_stop_and_start(self, mock_stop, mock_start):
        result = vast.restart_instance(7)

        self.assertTrue(result["ok"])
        mock_stop.assert_called_once_with(7)
        mock_start.assert_called_once_with(7)

    @patch("vast.requests.get")
    def test_get_ssh_details_extracts_host_and_port(self, mock_get):
        response = MagicMock()
        response.json.return_value = {
            "instance": {
                "id": 55,
                "public_ipaddr": "1.2.3.4",
                "ports": {"22/tcp": ["41022"]},
                "actual_status": "running",
            }
        }
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        details = vast.get_ssh_details(55)

        self.assertEqual(details["ssh_host"], "1.2.3.4")
        self.assertEqual(details["ssh_port"], 41022)
        self.assertEqual(details["status"], "running")

    @patch("vast.os.path.expanduser", return_value="/home/test/.ssh/id_ed25519")
    def test_build_ssh_command_uses_configured_key(self, mock_expanduser):
        command = vast.build_ssh_command("ssh4.vast.ai", 30123)

        self.assertEqual(
            command,
            "ssh -o StrictHostKeyChecking=no -p 30123 -i /home/test/.ssh/id_ed25519 root@ssh4.vast.ai",
        )
        mock_expanduser.assert_called_once()

    @patch("vast.requests.put")
    def test_deploy_on_offer_bootstraps_drayhub_platform_repo(self, mock_put):
        response = MagicMock()
        response.ok = True
        response.json.return_value = {"new_contract": 123}
        mock_put.return_value = response
        vast.CONFIG["model"] = "brian-mistral"
        vast.CONFIG["vast_admin_password"] = "secret-pass"

        vast.deploy_on_offer(999, disk_gb=50)

        payload = mock_put.call_args.kwargs["json"]
        self.assertEqual(payload["disk"], 50)
        self.assertEqual(payload["label"], "aion")
        self.assertIn("https://github.com/draygen/drayhub-platform.git", payload["onstart"])
        self.assertIn("cd /workspace/drayhub-platform/services/aion", payload["onstart"])
        self.assertIn("requirements.runtime.txt", payload["onstart"])
        self.assertIn('"model": "brian-mistral"', payload["onstart"])


if __name__ == "__main__":
    unittest.main()
