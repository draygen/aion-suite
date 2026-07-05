import unittest
from unittest.mock import patch

import web


class TestWebRuntime(unittest.TestCase):
    def test_main_uses_configured_runtime_without_forcing_debug(self):
        with patch.object(web, "logger") as mock_logger, patch.object(web.app, "run") as mock_run:
            with patch.dict(web.CONFIG, {"DEBUG": False, "web_host": "127.0.0.1", "web_port": 5050}, clear=False):
                web.run_web_app()

        mock_run.assert_called_once_with(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
        mock_logger.info.assert_any_call("Starting Aion web server.")


if __name__ == "__main__":
    unittest.main()
