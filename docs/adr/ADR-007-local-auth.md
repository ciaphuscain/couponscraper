# ADR-007: Loopback-only server with token, session cookie and CSRF

**Status:** accepted

**Decision.** Bind 127.0.0.1 only (other addresses are refused). The API requires
`X-CouponHunter-Token` (random 256-bit file, owner-only permissions). The dashboard requires a
session cookie derived by HMAC from the token (HttpOnly, SameSite=Strict), and every form POST
carries a derived CSRF token. A Host-header allowlist blocks DNS rebinding. A strict CSP with no
inline script is applied.

**Consequences.** Malicious websites can't read or drive the local API. Remote access is
deliberately unsupported; use a VPN (e.g. Tailscale) and keep loopback binding with a port
forward if ever needed.
