"""Logging configured to produce the exact one-line-per-check format asked for."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _Formatter(logging.Formatter):
    default_time_format = "%Y-%m-%d %H:%M:%S"
    default_msec_format = None

    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record)} — {record.getMessage()}"
        if record.levelno >= logging.WARNING and record.levelname != "WARNING":
            base = f"{self.formatTime(record)} — [{record.levelname}] {record.getMessage()}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = _Formatter()

    # "La Guérite" and the em-dash separator need UTF-8. Windows consoles and
    # some container runtimes default to a narrower codepage; ask for UTF-8 and
    # fall back to replacement characters rather than crashing a log call.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("Could not open log file %s (%s); logging to stdout only", log_file, exc)

    # Third-party chatter we do not need.
    for noisy in ("urllib3", "requests", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
