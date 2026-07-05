import unittest
from unittest.mock import patch

import ollama_endpoints
from config import CONFIG


class OllamaEndpointsTests(unittest.TestCase):
    def test_candidates_include_wsl_routes(self):
        original = CONFIG.get("OLLAMA_BASE_URL")
        CONFIG["OLLAMA_BASE_URL"] = "http://192.168.0.2:11434"
        try:
            with patch.object(ollama_endpoints, "_wsl_gateway_host", return_value="172.29.48.1"):
                urls = ollama_endpoints.ollama_base_urls()
            self.assertEqual(urls[0], "http://192.168.0.2:11434")
            self.assertIn("http://host.docker.internal:11434", urls)
            self.assertIn("http://localhost:11434", urls)
            self.assertIn("http://127.0.0.1:11434", urls)
            self.assertIn("http://172.29.48.1:11434", urls)
        finally:
            CONFIG["OLLAMA_BASE_URL"] = original

    def test_zero_host_is_not_used_as_client_target(self):
        original = CONFIG.get("OLLAMA_BASE_URL")
        CONFIG["OLLAMA_BASE_URL"] = "http://0.0.0.0:11434"
        try:
            with patch.object(ollama_endpoints, "_wsl_gateway_host", return_value=""):
                urls = ollama_endpoints.ollama_base_urls()
            self.assertNotIn("http://0.0.0.0:11434", urls)
            self.assertIn("http://localhost:11434", urls)
        finally:
            CONFIG["OLLAMA_BASE_URL"] = original


if __name__ == "__main__":
    unittest.main()
