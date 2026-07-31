"""Structured logging configuration for Wren-LangChain FastAPI server."""

from __future__ import annotations

import os
import sys
import logging
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class StructuredRequestFormatter(logging.Formatter):
    """Custom formatter to inject [req=<request_id>] into log records."""

    def format(self, record: logging.LogRecord) -> str:
        record.req_id = request_id_var.get("-")
        return super().format(record)


def setup_logging() -> logging.Logger:
    """Initialize structured logging using stdlib logging module."""
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    logger = logging.getLogger("wren")
    logger.setLevel(log_level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(log_level)
        formatter = StructuredRequestFormatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] [req=%(req_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


logger = setup_logging()


def get_logger(submodule: str) -> logging.Logger:
    """Get sub-logger under the 'wren' namespace."""
    return logging.getLogger(f"wren.{submodule}")
