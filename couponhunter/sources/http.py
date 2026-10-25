"""A deliberately boring HTTP client for permitted sources.

Properties that matter more than features:
  * HTTPS only, host pinned to the source's configured host (no open SSRF);
  * redirects only within the same host;
  * hard timeout and response-size cap;
  * bounded retries with exponential backoff + jitter on transient errors;
  * 429 honours Retry-After and stops; 401/403 stop and disable the source;
  * robots.txt is honoured; the User-Agent names the tool honestly.
There is no proxy rotation, header spoofing or CAPTCHA handling, by design.
"""

from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urlsplit

from .base import SourceError

USER_AGENT = "CouponHunter/0.1 (personal, low-frequency feed reader)"
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 10


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host: str):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme != "https" or (parts.hostname or "").lower() != self.host:
            raise SourceError(f"refusing cross-host redirect to {parts.hostname}", kind="config")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    def __init__(self, allowed_host: str, retries: int = 2, backoff_s: float = 1.0, sleep=time.sleep,
                 opener_factory=None, check_robots: bool = True):
        self.host = allowed_host.lower()
        self.retries = retries
        self.backoff_s = backoff_s
        self.sleep = sleep
        self._opener = (opener_factory or (lambda h: urllib.request.build_opener(_SameHostRedirect(h))))(self.host)
        self.check_robots = check_robots
        self._robots: urllib.robotparser.RobotFileParser | None = None

    def _check_url(self, url: str) -> None:
        p = urlsplit(url)
        if p.scheme != "https" or (p.hostname or "").lower() != self.host or p.username or p.port:
            raise SourceError(f"URL not permitted for this source: {url[:120]}", kind="config")

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.check_robots:
            return True
        if self._robots is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                body = self._get_once(f"https://{self.host}/robots.txt")
                rp.parse(body.decode("utf-8", "replace").splitlines())
            except SourceError as e:
                if e.kind == "not_found":
                    rp.parse([])  # no robots.txt: no restrictions declared
                else:
                    raise
            self._robots = rp
        return self._robots.can_fetch(USER_AGENT, url)

    def _get_once(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with self._opener.open(req, timeout=TIMEOUT_S) as resp:
                body = resp.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SourceError(f"HTTP {e.code}: access refused", kind="auth") from e
            if e.code == 404:
                raise SourceError("HTTP 404", kind="not_found") from e
            if e.code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                raise SourceError("HTTP 429: rate limited", kind="rate_limited",
                                  retry_after_s=int(ra) if ra and ra.isdigit() else 3600) from e
            raise SourceError(f"HTTP {e.code}", kind="transient" if e.code >= 500 else "config") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise SourceError(f"network error: {e}", kind="transient") from e
        if len(body) > MAX_BYTES:
            raise SourceError("response exceeds size cap", kind="schema")
        return body

    def get(self, url: str) -> bytes:
        self._check_url(url)
        if not self._allowed_by_robots(url):
            raise SourceError("robots.txt disallows this URL", kind="auth")
        attempt = 0
        while True:
            try:
                return self._get_once(url)
            except SourceError as e:
                if e.kind != "transient" or attempt >= self.retries:
                    raise
                delay = self.backoff_s * (2 ** attempt) * (0.5 + random.random())
                self.sleep(delay)
                attempt += 1
