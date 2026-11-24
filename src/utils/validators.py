"""Input validation: URLs (SSRF protection), URL canonicalisation, text helpers."""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Awaitable, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.core.exceptions import UnsafeURLError

ALLOWED_SCHEMES = {"http", "https"}
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid",
    "ref", "refid", "trk", "trackingid", "src", "source", "gh_src", "lever-source", "lever-origin",
    "originalsubdomain", "mc_cid", "mc_eid", "_ga", "igshid", "si",
}

Resolver = Callable[[str], Awaitable[list[str]]]


def validate_url_syntax(url: str) -> str:
    """Validate scheme/host syntax. Returns the stripped URL or raises UnsafeURLError."""
    if not isinstance(url, str):
        raise UnsafeURLError("URL must be a string")
    url = url.strip()
    if len(url) > 2048:
        raise UnsafeURLError("URL too long")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError("Invalid URL syntax") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Scheme not allowed: {parts.scheme or '(none)'}")
    if not parts.hostname:
        raise UnsafeURLError("URL has no host")
    if parts.username or parts.password:
        raise UnsafeURLError("Credentials in URL are not allowed")
    host = parts.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeURLError("Local host names are not allowed")
    try:
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise UnsafeURLError("Invalid port")
    except ValueError as exc:
        raise UnsafeURLError("Invalid port") from exc
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None and not is_public_ip(str(ip)):
        raise UnsafeURLError("Non-public IP address")
    return url


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


async def system_resolver(host: str) -> list[str]:
    import asyncio

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def validate_url_safe(url: str, resolver: Resolver | None = None) -> str:
    """Full SSRF validation: syntax + every resolved address must be public."""
    url = validate_url_syntax(url)
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return url  # literal IP already checked
    except ValueError:
        pass
    resolver = resolver or system_resolver
    try:
        addrs = await resolver(host)
    except OSError as exc:
        raise UnsafeURLError(f"DNS resolution failed for {host}") from exc
    if not addrs:
        raise UnsafeURLError(f"DNS returned no addresses for {host}")
    for a in addrs:
        if not is_public_ip(a):
            raise UnsafeURLError(f"{host} resolves to a non-public address")
    return url


def canonical_url(url: str | None) -> str:
    """Canonical form for de-duplication: lowercase host, no fragment, no tracking params."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k.lower() not in _TRACKING_PARAMS]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_matches(url_or_host: str, domains: Iterable[str]) -> bool:
    host = domain_of(url_or_host) if "/" in url_or_host else url_or_host.lower()
    for d in domains:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


_MOJIBAKE_MAP = (
    ("â€™", "’"),
    ("â€˜", "‘"),
    ("â€œ", "“"),
    ("â€\x9d", "”"),
    ("â€\x9c", "“"),
    ("â€“", "–"),
    ("â€”", "—"),
    ("â€¢", "•"),
    ("â€¦", "…"),
    ("Â\xa0", " "),
    ("\xa0", " "),
)


def repair_mojibake(text: str) -> str:
    """Repair common UTF-8-decoded-as-Latin-1 punctuation artifacts."""
    if not text or ("â€" not in text and "Â\xa0" not in text and "\xa0" not in text):
        return text
    for bad, good in _MOJIBAKE_MAP:
        if bad in text:
            text = text.replace(bad, good)
    return text


def clean_text(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    t = repair_mojibake(text)
    t = re.sub(r"[​-‏  ﻿]", "", t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()
    if limit and len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t
