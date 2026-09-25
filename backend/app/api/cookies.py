"""The session cookie's attributes, in one place (ADR 0022): HttpOnly (no
JavaScript access to the token), Secure outside development/test,
SameSite=Lax, Path=/, host-only unless AUTH_COOKIE_DOMAIN is set."""

from __future__ import annotations

from starlette.responses import Response

from app.core.config import Settings


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_session_ttl_hours * 3600,
        path="/",
        domain=settings.auth_cookie_domain or None,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        domain=settings.auth_cookie_domain or None,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
