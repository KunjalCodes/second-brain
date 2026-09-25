"""Request/response schemas for `/auth/*` (Phase 9, docs/PHASE9_PLAN.md
section 9). Every request model is `extra="forbid"`: a client-supplied
`user_id` is a 422, never an identity. Request strings are bounded here; the
password POLICY (12-128 chars) is enforced by the service so login never
reveals it."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_MAX_INPUT = 1024


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=_MAX_INPUT)
    password: str = Field(min_length=1, max_length=_MAX_INPUT)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=_MAX_INPUT)
    password: str = Field(min_length=1, max_length=_MAX_INPUT)
    #: Non-browser clients (tests, scripts, eval) ask for the token in the body
    #: to send as `Authorization: Bearer`. Browsers rely on the HttpOnly cookie.
    token_in_body: bool = False


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=_MAX_INPUT)
    new_password: str = Field(min_length=1, max_length=_MAX_INPUT)


class UserView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    email: str | None
    status: str
    created_at: datetime | None


class SessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_at: datetime
    idle_expires_at: datetime


class RegisterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: UserView


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: UserView
    session: SessionView
    token: str | None = None


class MeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: UserView
    session: SessionView | None
    auth_mode: Literal["session", "dev_header"]
