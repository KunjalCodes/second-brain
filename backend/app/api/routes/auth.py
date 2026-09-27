"""Authentication endpoints (Phase 9, docs/PHASE9_PLAN.md section 9).

No refresh endpoint (the idle window slides on use), no session listing
(Q10), no OAuth, no email flows. Every `user_id` here is chosen by the
server; no request model can carry one.
"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.cookies import clear_session_cookie, set_session_cookie
from app.api.deps import (
    AuthContext,
    AuthContextDep,
    AuthServiceDep,
    RateLimiterDep,
    SettingsDep,
)
from app.auth.http import client_ip, extract_credential, origin_allowed
from app.auth.passwords import normalize_email
from app.core.config import Settings
from app.core.errors import (
    AuthenticationRequiredError,
    OriginNotAllowedError,
    RateLimitedError,
)
from app.repositories.auth_repository import AccountRecord
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    PasswordChangeRequest,
    RegisterRequest,
    RegisterResponse,
    SessionView,
    UserView,
)
from app.services.auth_service import Authenticated
from app.services.rate_limit_service import LimitClass, RateLimiter

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_view(account: AccountRecord) -> UserView:
    return UserView(
        id=account.user_id,
        email=account.email,
        status=account.status,
        created_at=account.created_at,
    )


def _network_bucket(
    request: Request, settings: Settings, per_ip: LimitClass, global_class: LimitClass
) -> tuple[LimitClass, str]:
    """The per-IP bucket when the client IP is trustworthy; otherwise one
    explicit service-wide bucket (Phase 11, D5) -- never a per-IP bucket that
    is secretly shared."""
    if settings.rate_limit_per_ip:
        return per_ip, f"ip:{client_ip(request, settings)}"
    return global_class, "global"


def _email_bucket(email: str) -> str:
    """The per-email rate-limit identity: a digest, never the raw address."""
    try:
        normalized = normalize_email(email)
    except Exception:  # noqa: BLE001 - malformed emails share one bucket
        normalized = "<malformed>"
    return "email:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def require_allowed_origin_if_present(request: Request, settings: SettingsDep) -> None:
    """Login/register CSRF: a browser request from a disallowed origin is
    refused. Requests without `Origin` (non-browser clients) are allowed --
    these endpoints do not act on an existing cookie."""
    if request.headers.get("origin") and not origin_allowed(request, settings):
        raise OriginNotAllowedError()


def require_session(context: AuthContextDep) -> AuthContext:
    """Endpoints that act on a SESSION (logout-all, password change) need a
    real session credential; the dev-header placeholder has none."""
    if context.authenticated is None:
        raise AuthenticationRequiredError()
    return context


SessionContext = Annotated[AuthContext, Depends(require_session)]


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_allowed_origin_if_present)],
    summary="Create an account (disabled by default in production)",
)
async def register(
    payload: RegisterRequest,
    request: Request,
    settings: SettingsDep,
    auth: AuthServiceDep,
    limiter: RateLimiterDep,
) -> RegisterResponse:
    if not settings.registration_enabled:
        # Indistinguishable from an unknown route.
        raise HTTPException(status_code=404, detail="Not Found")
    await limiter.enforce(
        *_network_bucket(
            request, settings, LimitClass.AUTH_REGISTER, LimitClass.AUTH_REGISTER_GLOBAL
        )
    )
    account = await auth.register(email=payload.email, password=payload.password)
    return RegisterResponse(user=_user_view(account))


async def _enforce_login_limits(
    limiter: RateLimiter, request: Request, settings: Settings, email: str
) -> None:
    # Count BOTH buckets on every attempt (each commits in its own
    # transaction), then enforce -- so neither is skipped on a block.
    network_class, network_identity = _network_bucket(
        request, settings, LimitClass.AUTH_LOGIN_IP, LimitClass.AUTH_LOGIN_GLOBAL
    )
    ip_decision = await limiter.hit(network_class, network_identity)
    email_decision = await limiter.hit(LimitClass.AUTH_LOGIN_EMAIL, _email_bucket(email))
    for limit_class, decision in (
        (network_class, ip_decision),
        (LimitClass.AUTH_LOGIN_EMAIL, email_decision),
    ):
        if decision is not None and not decision.allowed:
            raise RateLimitedError(
                limit_class=limit_class.value, retry_after_seconds=decision.retry_after_seconds
            )


@router.post(
    "/login",
    response_model=LoginResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_allowed_origin_if_present)],
    summary="Log in; sets the HttpOnly session cookie",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDep,
    auth: AuthServiceDep,
    limiter: RateLimiterDep,
) -> LoginResponse:
    await _enforce_login_limits(limiter, request, settings, payload.email)
    result = await auth.login(email=payload.email, password=payload.password)
    set_session_cookie(response, settings, result.token)
    return LoginResponse(
        user=_user_view(result.account),
        session=SessionView(
            expires_at=result.session.expires_at,
            idle_expires_at=auth.idle_expires_at(result.session),
        ),
        token=result.token if payload.token_in_body else None,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session (idempotent)",
)
async def logout(request: Request, settings: SettingsDep, auth: AuthServiceDep) -> Response:
    credential = extract_credential(request, settings)
    if credential is not None:
        if credential.via == "cookie" and not origin_allowed(request, settings):
            raise OriginNotAllowedError()
        authenticated: Authenticated | None
        try:
            authenticated = await auth.authenticate(credential.token)
        except AuthenticationRequiredError:
            authenticated = None  # already invalid: nothing to revoke
        if authenticated is not None:
            await auth.logout(session_id=authenticated.session.id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke every session of the current account",
)
async def logout_all(
    context: SessionContext, settings: SettingsDep, auth: AuthServiceDep
) -> Response:
    await auth.logout_all(user_id=context.user_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change password; revokes every other session",
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    context: SessionContext,
    settings: SettingsDep,
    auth: AuthServiceDep,
    limiter: RateLimiterDep,
) -> Response:
    assert context.authenticated is not None
    await _enforce_login_limits(limiter, request, settings, context.authenticated.account.email)
    await auth.change_password(
        user_id=context.user_id,
        current_session_id=context.authenticated.session.id,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeResponse, summary="The authenticated user")
async def me(context: AuthContextDep) -> MeResponse:
    if context.authenticated is None:
        return MeResponse(
            user=UserView(id=context.user_id, email=None, status="active", created_at=None),
            session=None,
            auth_mode="dev_header",
        )
    a = context.authenticated
    return MeResponse(
        user=_user_view(a.account),
        session=SessionView(expires_at=a.session.expires_at, idle_expires_at=a.idle_expires_at),
        auth_mode="session",
    )
