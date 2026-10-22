"""Structured logging: one JSON object per line on stderr.

Quiet by default (WARNING). Set RELAY_LOG_LEVEL=INFO to watch every model call
and tool result as the agent loop runs.
"""
from __future__ import annotations

import json
import logging
import os
import sys

_NAME = "relay"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, separators=(",", ":"), default=str)


def get_logger() -> logging.Logger:
    logger = logging.getLogger(_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        level = os.environ.get("RELAY_LOG_LEVEL", "WARNING").upper()
        logger.setLevel(getattr(logging, level, logging.WARNING))
        logger.propagate = False
    return logger


def event(name: str, level: int = logging.INFO, **fields: object) -> None:
    get_logger().log(level, name, extra={"fields": fields})
