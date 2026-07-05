import importlib
import logging
import os
import tempfile
import unittest

import config


class TestAionLogging(unittest.TestCase):
    def test_configure_logging_writes_to_rotating_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "aion.log")
            original = {
                "log_file": config.CONFIG.get("log_file"),
                "log_level": config.CONFIG.get("log_level"),
                "DEBUG": config.CONFIG.get("DEBUG"),
            }
            config.CONFIG["log_file"] = log_path
            config.CONFIG["log_level"] = "INFO"
            config.CONFIG["DEBUG"] = False

            try:
                aion_logging = importlib.import_module("aion_logging")
                aion_logging = importlib.reload(aion_logging)

                logger = aion_logging.configure_logging()
                logger.info("logging smoke test")

                self.assertTrue(os.path.exists(log_path))
                with open(log_path, "r", encoding="utf-8") as handle:
                    contents = handle.read()
                self.assertIn("logging smoke test", contents)
                self.assertEqual(logger.level, logging.INFO)
            finally:
                config.CONFIG.update(original)


if __name__ == "__main__":
    unittest.main()
