"""Runtime configuration: where data lives, which port, which secrets.

Secrets (API token, Telegram token) are never stored in the database or in
the repository. The API token is a random file in the data directory created
on first run; the Telegram token comes from environment variables.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def default_home() -> Path:
    if env := os.environ.get("COUPONHUNTER_HOME"):
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "CouponHunter"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "couponhunter"


@dataclass(frozen=True)
class Config:
    home: Path
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def db_path(self) -> Path:
        return self.home / "couponhunter.db"

    @property
    def backup_dir(self) -> Path:
        return self.home / "backups"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def token_path(self) -> Path:
        return self.home / "api_token"

    def api_token(self) -> str:
        """Read or create the local API token (owner-only file permissions where supported)."""
        self.home.mkdir(parents=True, exist_ok=True)
        if self.token_path.exists():
            tok = self.token_path.read_text(encoding="utf-8").strip()
            if len(tok) >= 32:
                return tok
        tok = secrets.token_urlsafe(32)
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
        return tok


def load(home: str | Path | None = None, port: int | None = None) -> Config:
    cfg_port = port or int(os.environ.get("COUPONHUNTER_PORT", "8765"))
    host = os.environ.get("COUPONHUNTER_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(
            "Refusing to bind to a non-loopback address. CouponHunter has no internet-grade "
            "authentication; expose it remotely only through a VPN such as Tailscale."
        )
    return Config(home=Path(home) if home else default_home(), host=host, port=cfg_port)


def derive(token: str, purpose: str) -> str:
    """Derived secrets (CSRF, session cookie) so the raw token never sits in HTML or cookies."""
    return hmac.new(token.encode(), purpose.encode(), hashlib.sha256).hexdigest()
