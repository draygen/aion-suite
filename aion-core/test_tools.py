import unittest
from unittest.mock import patch

from config import CONFIG
from tools import (
    ToolRuntimeError,
    available_tool_status,
    dispatch_safe_diagnostic_message,
    dispatch_tool_message,
    handle_ops_command,
    is_authorized_target,
)


class TestTools(unittest.TestCase):
    def setUp(self):
        self._authorized = list(CONFIG.get("authorized_network_targets") or [])
        self._enabled = CONFIG.get("network_ops_enabled", True)
        CONFIG["authorized_network_targets"] = ["localhost", "127.0.0.1", "*.example.internal", "app.example.com"]
        CONFIG["network_ops_enabled"] = True

    def tearDown(self):
        CONFIG["authorized_network_targets"] = self._authorized
        CONFIG["network_ops_enabled"] = self._enabled

    def test_authorizes_private_and_explicit_targets(self):
        self.assertTrue(is_authorized_target("127.0.0.1"))
        self.assertTrue(is_authorized_target("app.example.com"))
        self.assertTrue(is_authorized_target("db.example.internal"))

    def test_blocks_unknown_public_targets(self):
        self.assertFalse(is_authorized_target("google.com"))
        self.assertFalse(is_authorized_target("8.8.8.8"))
        self.assertTrue(is_authorized_target("10.0.0.5"))

    def test_authorizes_cidr_targets(self):
        CONFIG["authorized_network_targets"] = ["203.0.113.0/24"]
        self.assertTrue(is_authorized_target("203.0.113.10"))
        self.assertFalse(is_authorized_target("203.0.114.10"))

    def test_authorizes_ipv4_cidr_with_mixed_family_allowlist(self):
        CONFIG["authorized_network_targets"] = ["::1", "192.168.0.0/24"]
        self.assertTrue(is_authorized_target("192.168.0.0/24"))
        self.assertFalse(is_authorized_target("192.168.1.0/24"))

    def test_authorizes_ipv6_cidr_with_mixed_family_allowlist(self):
        CONFIG["authorized_network_targets"] = ["127.0.0.1", "fd00::/64"]
        self.assertTrue(is_authorized_target("fd00::/80"))
        self.assertFalse(is_authorized_target("fd01::/64"))

    @patch("tools.run_nmap_ping_sweep", return_value="hosts")
    def test_routes_ping_sweep_with_mixed_family_allowlist(self, mock_sweep):
        CONFIG["authorized_network_targets"] = ["::1", "192.168.0.0/24"]
        result = dispatch_tool_message("discover hosts 192.168.0.0/24", "127.0.0.1")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_id, "nmap_ping_sweep")
        mock_sweep.assert_called_once_with("192.168.0.0/24")

    @patch("tools.run_nmap_ping_sweep", side_effect=TypeError("mixed address family"))
    def test_wraps_registered_tool_runtime_failures(self, mock_sweep):
        with self.assertRaises(ToolRuntimeError) as raised:
            dispatch_tool_message("discover hosts 192.168.0.0/24", "127.0.0.1")
        self.assertEqual(raised.exception.tool_id, "nmap_ping_sweep")
        self.assertEqual(raised.exception.error_type, "TypeError")

    @patch("tools.run_ping", return_value="ok")
    def test_routes_ping_command(self, mock_ping):
        result = handle_ops_command("ping app.example.com", "1.2.3.4")
        self.assertIn("PING for app.example.com", result)
        mock_ping.assert_called_once_with("app.example.com")

    @patch("tools.run_nmap_service_scan", return_value="scan result")
    def test_routes_nmap_command(self, mock_nmap):
        result = handle_ops_command("scan app.example.com", "1.2.3.4")
        self.assertIn("Nmap service scan", result)
        mock_nmap.assert_called_once_with("app.example.com")

    @patch("tools.run_nmap_service_scan", return_value="scan result")
    def test_routes_web_scan_url(self, mock_nmap):
        result = handle_ops_command("web scan https://app.example.com:5000/health", "1.2.3.4")
        self.assertIn("Nmap service scan", result)
        mock_nmap.assert_called_once_with("app.example.com")

    @patch("tools.run_httpx", return_value="ok")
    def test_routes_httpx_url(self, mock_httpx):
        result = handle_ops_command("httpx https://app.example.com/login", "1.2.3.4")
        self.assertIn("httpx for app.example.com", result)
        mock_httpx.assert_called_once_with("https://app.example.com/login")

    @patch("tools.run_ping", return_value="ok")
    def test_dispatch_tool_message_returns_structured_execution(self, mock_ping):
        result = dispatch_tool_message("ping app.example.com", "1.2.3.4")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_id, "ping")
        self.assertEqual(result.args["target"], "app.example.com")
        self.assertIn("PING for app.example.com", result.output)
        mock_ping.assert_called_once_with("app.example.com")

    def test_dispatch_tool_message_returns_none_for_regular_chat(self):
        self.assertIsNone(dispatch_tool_message("tell me a joke", "1.2.3.4"))

    @patch("tools.run_ping", return_value="ok")
    def test_safe_diagnostic_dispatch_runs_allowlisted_ping(self, mock_ping):
        result = dispatch_safe_diagnostic_message("ping app.example.com", "127.0.0.1")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_id, "ping")
        mock_ping.assert_called_once_with("app.example.com")

    @patch("tools.run_nmap_service_scan", return_value="must not run")
    def test_safe_diagnostic_dispatch_rejects_service_scan(self, mock_scan):
        self.assertIsNone(
            dispatch_safe_diagnostic_message("scan app.example.com", "127.0.0.1")
        )
        mock_scan.assert_not_called()

    @patch("tools._tool_installed", side_effect=lambda name: name in {"ping", "nmap"})
    @patch("tools._first_installed", return_value=None)
    def test_available_tool_status(self, mock_first, mock_installed):
        tools = available_tool_status()
        labels = {tool["label"]: tool["installed"] for tool in tools}
        self.assertTrue(labels["Ping"])
        self.assertTrue(labels["Nmap"])
        self.assertFalse(labels["OWASP ZAP"])

    @patch("tools.run_firecrawl_search", return_value="Title: X\nURL: https://x")
    def test_routes_firecrawl_web_search(self, mock_search):
        result = handle_ops_command("web search ollama think false", "1.2.3.4")
        self.assertIn("Title: X", result)
        mock_search.assert_called_once_with("ollama think false")

    @patch("tools.run_firecrawl_scrape", return_value="# clean markdown")
    def test_routes_firecrawl_scrape(self, mock_scrape):
        result = handle_ops_command("scrape https://example.com/docs", "1.2.3.4")
        self.assertIn("clean markdown", result)
        mock_scrape.assert_called_once_with("https://example.com/docs")

    def test_bare_search_does_not_hit_firecrawl(self):
        # "search <query>" (no "web"/"the web") must not route to firecrawl.
        result = dispatch_tool_message("search the quietest keyboard", "1.2.3.4")
        self.assertNotEqual(getattr(result, "tool_id", None), "firecrawl_search")

    @patch("tools._firecrawl_post", return_value={"data": {"web": [
        {"title": "Doc", "url": "https://d", "description": "desc"}]}})
    def test_firecrawl_search_formats_results(self, mock_post):
        from tools import run_firecrawl_search
        out = run_firecrawl_search("q")
        self.assertIn("Title: Doc", out)
        self.assertIn("https://d", out)

    def test_firecrawl_reports_missing_key(self):
        from tools import run_firecrawl_search
        saved = CONFIG.get("firecrawl_api_key")
        CONFIG["firecrawl_api_key"] = ""
        try:
            out = run_firecrawl_search("q")
        finally:
            CONFIG["firecrawl_api_key"] = saved
        self.assertIn("Firecrawl API key not configured", out)

    def test_returns_none_when_disabled(self):
        CONFIG["network_ops_enabled"] = False
        self.assertIsNone(handle_ops_command("ping app.example.com", "1.2.3.4"))

    def test_returns_help_for_unsupported_command(self):
        result = handle_ops_command("run authorized check", "1.2.3.4")
        self.assertIn("Unsupported command.", result)


if __name__ == "__main__":
    unittest.main()
