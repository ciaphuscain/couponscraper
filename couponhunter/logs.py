"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from pathlib import Path

_REDACTIONS = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<telegram-token>"),
    (re.compile(r"(?i)(token|authorization|x-couponhunter-token|cookie)([\"'=: ]+)[^\s\"',;]+"), r"\1\2<redacted>"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "<card-like-number>"),
]


def redact(s: str) -> str:
    for pat, repl in _REDACTIONS:
        s = pat.sub(repl, s)
    return s


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            out.update({k: redact(str(v)) if isinstance(v, str) else v for k, v in extra.items()})
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False, default=str)


def setup(log_dir: Path | None, level: int = logging.INFO, console: bool = True) -> None:
    root = logging.getLogger("couponhunter")
    root.setLevel(level)
    root.handlers.clear()
    fmt = JsonFormatter()
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "couponhunter.log", maxBytes=5_000_000, backupCount=5,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"couponhunter.{name}")


def event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
