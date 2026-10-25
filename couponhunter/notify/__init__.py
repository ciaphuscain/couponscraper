"""Notification channels. A channel only delivers text; alerting policy
(thresholds, dedup, cooldown, quiet hours) lives in alerts.py."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol


class NotifyError(Exception):
    def __init__(self, message: str, retry_after_s: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.permanent = permanent


class Notifier(Protocol):
    name: str

    def send(self, text: str) -> None: ...


class ConsoleNotifier:
    name = "console"

    def __init__(self, stream=None):
        import sys
        self.stream = stream or sys.stdout

    def send(self, text: str) -> None:
        self.stream.write("\n=== CouponHunter alert ===\n" + text + "\n")
        self.stream.flush()


class FileNotifier:
    """Appends alerts to a local file (JSON lines). Useful with no bot configured."""

    name = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def send(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")


class TelegramNotifier:
    """Telegram Bot API sendMessage. Plain text only (no parse_mode), so offer
    titles from third-party sources cannot inject formatting or links.

    The token is read from the environment, never stored in the database or
    logged. Telegram asks bots to keep to ~1 message/second per chat; alert
    grouping keeps us far below that.
    """

    name = "telegram"
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str, opener=None):
        if not token or not chat_id:
            raise ValueError("telegram token and chat id are required")
        self._token = token
        self.chat_id = chat_id
        self._opener = opener or urllib.request.build_opener()

    @classmethod
    def from_env(cls) -> "TelegramNotifier | None":
        token = os.environ.get("COUPONHUNTER_TELEGRAM_TOKEN")
        chat = os.environ.get("COUPONHUNTER_TELEGRAM_CHAT_ID")
        return cls(token, chat) if token and chat else None

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self.chat_id!r}, token=<redacted>)"

    def send(self, text: str) -> None:
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"{self.API}/bot{self._token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=10) as resp:
                data = json.loads(resp.read(65_536) or b"{}")
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read(65_536) or b"{}")
            except ValueError:
                data = {}
            retry = (data.get("parameters") or {}).get("retry_after")
            permanent = e.code in (400, 401, 403, 404)
            raise NotifyError(f"telegram HTTP {e.code}: {data.get('description', '')[:200]}",
                              retry_after_s=retry, permanent=permanent) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NotifyError(f"telegram unreachable: {type(e).__name__}") from None
        if not data.get("ok"):
            raise NotifyError(f"telegram error: {str(data.get('description'))[:200]}")
