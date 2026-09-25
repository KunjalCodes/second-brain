"""HTTP-level identity helpers: where a credential comes from, whether a
browser request's Origin is allowed (the CSRF boundary, ADR 0022), and which
client IP a rate limit should key on (ADR 0023)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Literal

from starlette.requests import Request

from app.core.config import Settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class PresentedCredential:
    token: str
    via: Literal["bearer", "cookie"]


def extract_credential(request: Request, settings: Settings) -> PresentedCredential | None:
    """`Authorization: Bearer` wins over the session cookie. Any Authorization
    header counts as an attempt: a malformed one yields an empty token, which
    later fails verification (401) rather than silently falling through to a
    weaker identity source."""
    header = request.headers.get("authorization")
    if header is not None:
        scheme, _, value = header.partition(" ")
        token = value.strip() if scheme.lower() == "bearer" else ""
        return PresentedCredential(token=token, via="bearer")
    cookie = request.cookies.get(settings.auth_cookie_name)
    if cookie:
        return PresentedCredential(token=cookie, via="cookie")
    return None


def origin_allowed(request: Request, settings: Settings) -> bool:
    """True iff `Origin` is present and is an explicitly allowed origin, the
    configured single Vercel project's preview, or this API's own origin."""
    origin = request.headers.get("origin")
    if not origin:
        return False
    origin = origin.rstrip("/")
    if origin in settings.allowed_origins:
        return True
    regex = settings.allowed_origin_regex
    if regex and re.fullmatch(regex, origin):
        return True
    host = request.headers.get("host")
    return bool(host) and origin == f"{request.url.scheme}://{host}"


def _parse_trusted(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            networks.append(ipaddress.ip_network(item, strict=False))
    return networks


def _is_trusted(addr: str, networks: list) -> bool:  # noqa: ANN001
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def client_ip(request: Request, settings: Settings) -> str:
    """The direct peer, unless that peer is a configured trusted proxy -- only
    then is `X-Forwarded-For` consulted, taking the right-most address that is
    not itself a trusted proxy. An arbitrary client can therefore never choose
    its own rate-limit bucket by sending the header."""
    peer = request.client.host if request.client else "unknown"
    networks = _parse_trusted(settings.trusted_proxy_ips)
    if not networks or not _is_trusted(peer, networks):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for addr in reversed(hops):
        if not _is_trusted(addr, networks):
            try:
                return str(ipaddress.ip_address(addr))
            except ValueError:
                return peer  # garbage in the chain: fall back, never trust it
    return peer
