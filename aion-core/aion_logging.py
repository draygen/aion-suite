"""Centralized logging configuration for Aion."""
import logging
import os
from logging.handlers import RotatingFileHandler

from config import CONFIG

_LOGGER_NAME = "aion"
_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def _log_level() -> int:
    configured = str(CONFIG.get("log_level", "")).strip().upper()
    if configured:
        return getattr(logging, configured, logging.INFO)
    if CONFIG.get("DEBUG"):
        return logging.DEBUG
    return logging.INFO


def configure_logging() -> logging.Logger:
    """Configure the shared application logger once."""
    logger = logging.getLogger(_LOGGER_NAME)
    config_signature = (
        _log_level(),
        CONFIG.get("log_file", "data/logs/aion.log"),
        int(CONFIG.get("log_max_bytes", 1_000_000)),
        int(CONFIG.get("log_backup_count", 3)),
    )
    if getattr(logger, "_aion_configured", False) and getattr(logger, "_aion_signature", None) == config_signature:
        logger.setLevel(_log_level())
        return logger

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    logger.setLevel(_log_level())
    formatter = logging.Formatter(_DEFAULT_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_file = CONFIG.get("log_file", "data/logs/aion.log")
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=int(CONFIG.get("log_max_bytes", 1_000_000)),
        backupCount=int(CONFIG.get("log_backup_count", 3)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.propagate = False
    logger._aion_configured = True
    logger._aion_signature = config_signature
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    root = configure_logging()
    if not name or name == _LOGGER_NAME:
        return root
    return root.getChild(name)
