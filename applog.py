"""Console + persistent project-local logging (logs/job_finder.log, rotated).

Every formatted line passes through a redactor, so credentials from the
environment (Gmail app password, API keys/tokens) can never reach the console
or the log file, even inside an exception message.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "job_finder.log"
SECRET_ENV_VARS = ("GMAIL_APP_PASSWORD", "ADZUNA_APP_KEY", "ADZUNA_APP_ID", "APIFY_API_TOKEN", "RAPIDAPI_KEY")
_KEY_VALUE = re.compile(r"(?i)\b(app_?password|password|passwd|api[_-]?key|app_key|token|secret|authorization)"
                        r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
_HANDLER_TAG = "_job_finder_handler"


def redact(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name) or ""
        if len(value) >= 6:
            for variant in {value, value.replace(" ", "")}:
                text = text.replace(variant, "[REDACTED]")
    return _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: int = logging.INFO, log_file: Optional[Path] = None) -> Path:
    """Attach console + rotating-file handlers to the root logger (idempotent)."""
    log_file = Path(log_file or LOG_FILE)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()
    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
    handlers = [console]
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(RedactingFormatter(
            "%(asctime)s [%(levelname)s] %(process)d %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
        handlers.append(file_handler)
    except OSError as e:  # logging must never stop the program
        console.handle(logging.makeLogRecord({"msg": f"File logging unavailable: {e}", "levelno": logging.WARNING,
                                              "levelname": "WARNING", "name": "applog"}))
    for handler in handlers:
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
    return log_file
