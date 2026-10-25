"""Minimal escape-by-default HTML building.

Every dynamic value passes through esc(). Pages are served with a CSP that
forbids inline script entirely, so even an escaping bug cannot execute code.
"""

from __future__ import annotations

import html
from typing import Iterable

from ..money import fmt

NAV = [("/", "Overview"), ("/watchlist", "Watchlist"), ("/offers", "Offers"), ("/history", "Savings & history"),
       ("/health", "Health"), ("/settings", "Settings")]


class Raw(str):
    """Already-safe HTML produced by these helpers."""


def esc(v: object) -> str:
    if isinstance(v, Raw):
        return v
    return html.escape("" if v is None else str(v), quote=True)


def join(parts: Iterable[str]) -> Raw:
    return Raw("".join(esc(p) if not isinstance(p, Raw) else p for p in parts))


def tag(name: str, content: object = "", **attrs: object) -> Raw:
    a = "".join(f' {k.rstrip("_").replace("_", "-")}="{esc(v)}"' for k, v in attrs.items() if v is not None and v is not False)
    return Raw(f"<{name}{a}>{esc(content)}</{name}>")


def money(p: int | None) -> str:
    return "—" if p is None else fmt(p)


def pct(bp: int | None) -> str:
    return "—" if bp is None else f"{bp / 100:.0f}%"


def table(headers: list[str], rows: list[list[object]], empty: str = "Nothing here yet.") -> Raw:
    if not rows:
        return tag("p", empty, class_="muted")
    head = join(tag("th", h) for h in headers)
    body = join(tag("tr", join(tag("td", c) for c in r)) for r in rows)
    return Raw(f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def badge(text: str, kind: str = "") -> Raw:
    return tag("span", text, class_=f"badge {kind}".strip())


def link(href: str, text: object) -> Raw:
    return tag("a", text, href=href)


def form(action: str, csrf: str, fields: Raw | str, button: str, inline: bool = False) -> Raw:
    return Raw(
        f'<form method="post" action="{esc(action)}" class="{"inline" if inline else "stack"}">'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">{esc(fields) if not isinstance(fields, Raw) else fields}'
        f'<button type="submit">{esc(button)}</button></form>'
    )


def field(label: str, name: str, value: object = "", type_: str = "text", placeholder: str = "", required: bool = False) -> Raw:
    req = " required" if required else ""
    return Raw(f'<label>{esc(label)}<input type="{esc(type_)}" name="{esc(name)}" value="{esc(value)}" '
               f'placeholder="{esc(placeholder)}"{req}></label>')


def textarea(label: str, name: str, value: object = "", rows: int = 4) -> Raw:
    return Raw(f'<label>{esc(label)}<textarea name="{esc(name)}" rows="{rows}">{esc(value)}</textarea></label>')


def select(label: str, name: str, options: list[tuple[str, str]], selected: str = "") -> Raw:
    opts = "".join(f'<option value="{esc(v)}"{" selected" if v == selected else ""}>{esc(t)}</option>' for v, t in options)
    return Raw(f'<label>{esc(label)}<select name="{esc(name)}">{opts}</select></label>')


def page(title: str, body: Raw, active: str = "/", flash: str | None = None) -> str:
    nav = "".join(
        f'<a href="{esc(h)}"{" aria-current=page" if h == active else ""}>{esc(t)}</a>' for h, t in NAV
    )
    fl = f'<div class="flash" role="status">{esc(flash)}</div>' if flash else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} · CouponHunter</title><link rel=stylesheet href=/static/app.css></head>"
        f'<body><header><strong>CouponHunter</strong><nav>{nav}</nav></header><main>{fl}'
        f"<h1>{esc(title)}</h1>{body}</main></body></html>"
    )
