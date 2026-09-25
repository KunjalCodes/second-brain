"""Account and session persistence (Phase 9, docs/PHASE9_PLAN.md section 14).

Exempt from `tests/unit/test_sql_safety.py`'s `UserScopedRepository` rule, for
the same reason as `user_repository.py`: these rows are found BY CREDENTIAL
(a unique normalised email, or the SHA-256 of a session token) before any user
is known, so there is no caller-supplied user to scope by. Every read here is
by a unique key; every write is keyed by the already-resolved `user_id` or a
session id. Nothing in this module reads memory data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuthSession, User, UserAccount


@dataclass(frozen=True)
class AccountRecord:
    user_id: str
    email: str
    password_hash: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class SessionRecord:
    id: str
    user_id: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime
    revoked_at: datetime | None


def _account(row: UserAccount) -> AccountRecord:
    return AccountRecord(
        user_id=row.user_id,
        email=row.email_normalized,
        password_hash=row.password_hash,
        status=row.status,
        created_at=row.created_at,
    )


def _session(row: AuthSession) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        user_id=row.user_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


class AuthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # -- users (ownership rows) ------------------------------------------------

    async def user_exists(self, user_id: str) -> bool:
        row = await self._s.execute(select(User.id).where(User.id == user_id))
        return row.scalar_one_or_none() is not None

    async def create_user_row(self, user_id: str, *, now: datetime) -> None:
        await self._s.execute(
            insert(User)
            .values(id=user_id, created_at=now)
            .on_conflict_do_nothing(index_elements=["id"])
        )

    # -- accounts ----------------------------------------------------------------

    async def account_by_email(self, email_normalized: str) -> AccountRecord | None:
        row = await self._s.execute(
            select(UserAccount).where(UserAccount.email_normalized == email_normalized)
        )
        found = row.scalar_one_or_none()
        return _account(found) if found else None

    async def account_by_user_id(self, user_id: str) -> AccountRecord | None:
        found = await self._s.get(UserAccount, user_id)
        return _account(found) if found else None

    async def create_account(
        self, *, user_id: str, email_normalized: str, password_hash: str, now: datetime
    ) -> bool:
        """False if the email or the user id already has an account."""
        result = await self._s.execute(
            insert(UserAccount)
            .values(
                user_id=user_id,
                email_normalized=email_normalized,
                password_hash=password_hash,
                status="active",
                created_at=now,
                updated_at=now,
                password_changed_at=now,
            )
            .on_conflict_do_nothing()
            .returning(UserAccount.user_id)
        )
        return result.scalar_one_or_none() is not None

    async def set_password_hash(
        self, *, user_id: str, password_hash: str, now: datetime, changed: bool
    ) -> None:
        values: dict = {"password_hash": password_hash, "updated_at": now}
        if changed:
            values["password_changed_at"] = now
        await self._s.execute(
            update(UserAccount).where(UserAccount.user_id == user_id).values(**values)
        )

    async def set_status(self, *, user_id: str, status: str, now: datetime) -> None:
        await self._s.execute(
            update(UserAccount)
            .where(UserAccount.user_id == user_id)
            .values(status=status, updated_at=now)
        )

    async def record_login(self, *, user_id: str, now: datetime) -> None:
        await self._s.execute(
            update(UserAccount).where(UserAccount.user_id == user_id).values(last_login_at=now)
        )

    # -- sessions ----------------------------------------------------------------

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        token_sha256: str,
        now: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        row = AuthSession(
            id=session_id,
            user_id=user_id,
            token_sha256=token_sha256,
            created_at=now,
            expires_at=expires_at,
            last_used_at=now,
        )
        self._s.add(row)
        await self._s.flush()
        return _session(row)

    async def session_by_token_digest(
        self, token_sha256: str
    ) -> tuple[SessionRecord, AccountRecord] | None:
        row = await self._s.execute(
            select(AuthSession, UserAccount)
            .join(UserAccount, UserAccount.user_id == AuthSession.user_id)
            .where(AuthSession.token_sha256 == token_sha256)
        )
        found = row.first()
        if found is None:
            return None
        return _session(found[0]), _account(found[1])

    async def touch_session(self, *, session_id: str, now: datetime) -> None:
        await self._s.execute(
            update(AuthSession).where(AuthSession.id == session_id).values(last_used_at=now)
        )

    async def revoke_session(self, *, session_id: str, now: datetime) -> None:
        await self._s.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def revoke_user_sessions(
        self, *, user_id: str, now: datetime, except_session_id: str | None = None
    ) -> int:
        stmt = update(AuthSession).where(
            AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
        )
        if except_session_id is not None:
            stmt = stmt.where(AuthSession.id != except_session_id)
        result = await self._s.execute(stmt.values(revoked_at=now))
        return result.rowcount or 0
