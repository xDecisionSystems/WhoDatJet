"""Logging setup. `WHODATJET_LOG_FORMAT=json` emits one JSON object per line (for journald / log
shippers); the default is human-readable text. Extra fields passed via `extra={...}` are included."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = {k: v for k, v in record.__dict__.items() if k not in _STANDARD}
        return f"{base} {json.dumps(extra, default=str)}" if extra else base


def configure(verbose: bool = False, fmt: str | None = None) -> None:
    fmt = (fmt or os.environ.get("WHODATJET_LOG_FORMAT", "text")).lower()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("botocore", "boto3", "urllib3", "httpx", "httpcore", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
