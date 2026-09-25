"""Accounts and server-side sessions (Phase 9, docs/PHASE9_PLAN.md sections
6, 7, 9; ADRs 0020, 0021).

AUTH identity (email, password hash, sessions) is kept separate from MEMORY
OWNERSHIP identity (`users.id`). The value this service ultimately hands the
rest of the application is always a `users.id` chosen by the server --
a UUID4 at registration, or an existing legacy id attached by the operator CLI.

Every method opens its own short transaction(s) through `Database.session()`,
independent of any request-scoped session, so authentication state never
rides on (or is rolled back with) the protected request's transaction.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from app.auth.passwords import (
    ScryptParams,
    check_password_policy,
    hash_password,
    needs_rehash,
    normalize_email,
    verify_password,
)
from app.auth.tokens import is_well_formed, new_session_token, token_digest
from app.core.config import Settings
from app.core.errors import (
    AccountDisabledError,
    AuthenticationRequiredError,
    ConflictError,
    EmailTakenError,
    InvalidCredentialsError,
    NotFoundError,
)
from app.repositories.auth_repository import AccountRecord, AuthRepository, SessionRecord

logger = logging.getLogger(__name__)

ACTIVE = "active"
DISABLED = "disabled"


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _hash(password: str, params: ScryptParams) -> str:
    """scrypt is CPU-bound for ~0.3 s; run it in a worker thread so one login
    never blocks the event loop (every other request) for its whole duration.
    hashlib.scrypt releases the GIL, so concurrent logins really run in parallel
    (measured: 4 concurrent hashes ~ the time of 1)."""
    return await asyncio.to_thread(hash_password, password, params)


async def _verify(password: str, stored: str, *, maxmem: int) -> bool:
    return await asyncio.to_thread(verify_password, password, stored, maxmem=maxmem)


@lru_cache(maxsize=8)
def _dummy_hash(n: int, r: int, p: int, maxmem: int) -> str:
    """A fixed hash to verify against when the email is unknown, so an unknown
    account costs the same scrypt work as a wrong password (no timing oracle
    for account existence)."""
    return hash_password("dummy-password-for-timing", ScryptParams(n, r, p, maxmem))


@dataclass(frozen=True)
class LoginResult:
    account: AccountRecord
    session: SessionRecord
    token: str


@dataclass(frozen=True)
class Authenticated:
    account: AccountRecord
    session: SessionRecord
    idle_expires_at: datetime


class AuthService:
    def __init__(
        self,
        db,  # noqa: ANN001 - app.db.engine.Database
        settings: Settings,
        *,
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._now = now_fn

    # -- configuration -------------------------------------------------------------

    @property
    def _params(self) -> ScryptParams:
        s = self._settings
        return ScryptParams(s.auth_scrypt_n, s.auth_scrypt_r, s.auth_scrypt_p, s.auth_scrypt_maxmem)

    @property
    def _ttl(self) -> timedelta:
        return timedelta(hours=self._settings.auth_session_ttl_hours)

    @property
    def _idle(self) -> timedelta:
        return timedelta(hours=self._settings.auth_session_idle_hours)

    def idle_expires_at(self, session: SessionRecord) -> datetime:
        return min(session.last_used_at + self._idle, session.expires_at)

    # -- accounts -----------------------------------------------------------------

    async def register(self, *, email: str, password: str) -> AccountRecord:
        """Creates the ownership row (UUID4) and the account in ONE transaction."""
        email_n = normalize_email(email)
        check_password_policy(password, email_normalized=email_n)
        return await self._create_account(
            user_id=str(uuid.uuid4()), email_n=email_n, password=password, must_be_new=True
        )

    async def create_account_for_user(
        self, *, email: str, password: str, user_id: str | None
    ) -> AccountRecord:
        """Operator CLI path. With `user_id`, attaches a login to an EXISTING
        ownership id (e.g. `demo-user`), creating its `users` row if only Neo4j
        data exists. It never moves data between ids and refuses an id that
        already has an account, so it cannot reassign ownership."""
        email_n = normalize_email(email)
        check_password_policy(password, email_normalized=email_n)
        return await self._create_account(
            user_id=user_id or str(uuid.uuid4()),
            email_n=email_n,
            password=password,
            must_be_new=user_id is None,
        )

    async def _create_account(
        self, *, user_id: str, email_n: str, password: str, must_be_new: bool
    ) -> AccountRecord:
        password_hash = await _hash(password, self._params)
        now = self._now()
        async with self._db.session() as session:
            repo = AuthRepository(session)
            if await repo.account_by_email(email_n) is not None:
                raise EmailTakenError()
            if await repo.account_by_user_id(user_id) is not None:
                raise ConflictError(
                    "This ownership id already has an account", details={"user_id": user_id}
                )
            if must_be_new and await repo.user_exists(user_id):
                raise ConflictError("Generated user id collided; retry")
            await repo.create_user_row(user_id, now=now)
            if not await repo.create_account(
                user_id=user_id, email_normalized=email_n, password_hash=password_hash, now=now
            ):
                raise EmailTakenError()  # lost a concurrent race on the unique email
            account = await repo.account_by_user_id(user_id)
        assert account is not None
        logger.info("auth_account_created user_id=%s", user_id)
        return account

    async def account(self, email: str) -> AccountRecord:
        async with self._db.session() as session:
            found = await AuthRepository(session).account_by_email(normalize_email(email))
        if found is None:
            raise NotFoundError("No such account")
        return found

    async def set_status(self, *, email: str, status: str) -> AccountRecord:
        """Disabling revokes every session, so it takes effect immediately."""
        account = await self.account(email)
        now = self._now()
        async with self._db.session() as session:
            repo = AuthRepository(session)
            await repo.set_status(user_id=account.user_id, status=status, now=now)
            if status == DISABLED:
                await repo.revoke_user_sessions(user_id=account.user_id, now=now)
        logger.info("auth_account_status user_id=%s status=%s", account.user_id, status)
        return await self.account(email)

    async def reset_password(self, *, email: str, new_password: str) -> int:
        """Operator reset: sets the password and revokes ALL sessions."""
        account = await self.account(email)
        check_password_policy(new_password, email_normalized=account.email)
        new_hash = await _hash(new_password, self._params)
        now = self._now()
        async with self._db.session() as session:
            repo = AuthRepository(session)
            await repo.set_password_hash(
                user_id=account.user_id, password_hash=new_hash, now=now, changed=True
            )
            return await repo.revoke_user_sessions(user_id=account.user_id, now=now)

    async def revoke_all(self, *, email: str) -> int:
        account = await self.account(email)
        return await self.logout_all(user_id=account.user_id)

    # -- login / sessions ------------------------------------------------------------

    async def login(self, *, email: str, password: str) -> LoginResult:
        try:
            email_n = normalize_email(email)
        except Exception:  # noqa: BLE001 - a malformed email is just a failed login
            email_n = None
        account = None
        if email_n is not None:
            async with self._db.session() as session:
                account = await AuthRepository(session).account_by_email(email_n)
        maxmem = self._settings.auth_scrypt_maxmem
        if account is None:
            p = self._params
            dummy = await asyncio.to_thread(_dummy_hash, p.n, p.r, p.p, p.maxmem)
            await _verify(password, dummy, maxmem=maxmem)
            logger.info("auth_login_failed reason=unknown_account")
            raise InvalidCredentialsError()
        if not await _verify(password, account.password_hash, maxmem=maxmem):
            logger.info("auth_login_failed reason=wrong_password user_id=%s", account.user_id)
            raise InvalidCredentialsError()
        if account.status != ACTIVE:
            logger.info("auth_login_refused reason=disabled user_id=%s", account.user_id)
            raise AccountDisabledError()

        token = new_session_token()
        now = self._now()
        async with self._db.session() as session:
            repo = AuthRepository(session)
            if needs_rehash(account.password_hash, self._params):
                await repo.set_password_hash(
                    user_id=account.user_id,
                    password_hash=await _hash(password, self._params),
                    now=now,
                    changed=False,
                )
            record = await repo.create_session(
                session_id=str(uuid.uuid4()),
                user_id=account.user_id,
                token_sha256=token_digest(token),
                now=now,
                expires_at=now + self._ttl,
            )
            await repo.record_login(user_id=account.user_id, now=now)
        logger.info("auth_login user_id=%s session_id=%s", account.user_id, record.id)
        return LoginResult(account=account, session=record, token=token)

    async def authenticate(self, token: str, *, clear_cookie: bool = False) -> Authenticated:
        """Resolve a presented token to its live session and active account, or
        raise the ONE generic `AuthenticationRequiredError`. The specific cause
        goes to the server log only (never the token itself)."""

        def reject(reason: str, session_id: str | None = None) -> AuthenticationRequiredError:
            logger.info("auth_rejected reason=%s session_id=%s", reason, session_id)
            return AuthenticationRequiredError(clear_cookie=clear_cookie)

        if not token or not is_well_formed(token):
            raise reject("malformed")
        async with self._db.session() as session:
            found = await AuthRepository(session).session_by_token_digest(token_digest(token))
        if found is None:
            raise reject("unknown")
        record, account = found
        now = self._now()
        if record.revoked_at is not None:
            raise reject("revoked", record.id)
        if now >= record.expires_at:
            raise reject("expired", record.id)
        if now >= record.last_used_at + self._idle:
            raise reject("idle", record.id)
        if account.status != ACTIVE:
            raise reject("disabled", record.id)
        touch_every = self._settings.auth_session_touch_seconds
        if (now - record.last_used_at).total_seconds() >= touch_every:
            async with self._db.session() as session:
                await AuthRepository(session).touch_session(session_id=record.id, now=now)
            record = SessionRecord(
                id=record.id,
                user_id=record.user_id,
                created_at=record.created_at,
                expires_at=record.expires_at,
                last_used_at=now,
                revoked_at=None,
            )
        return Authenticated(
            account=account, session=record, idle_expires_at=self.idle_expires_at(record)
        )

    async def logout(self, *, session_id: str) -> None:
        async with self._db.session() as session:
            await AuthRepository(session).revoke_session(session_id=session_id, now=self._now())
        logger.info("auth_logout session_id=%s", session_id)

    async def logout_all(self, *, user_id: str) -> int:
        async with self._db.session() as session:
            n = await AuthRepository(session).revoke_user_sessions(user_id=user_id, now=self._now())
        logger.info("auth_logout_all user_id=%s revoked=%d", user_id, n)
        return n

    async def change_password(
        self, *, user_id: str, current_session_id: str, current_password: str, new_password: str
    ) -> int:
        """Verifies the current password; revokes every OTHER session."""
        async with self._db.session() as session:
            account = await AuthRepository(session).account_by_user_id(user_id)
        if account is None or not await _verify(
            current_password, account.password_hash, maxmem=self._settings.auth_scrypt_maxmem
        ):
            logger.info("auth_password_change_failed user_id=%s", user_id)
            raise InvalidCredentialsError()
        check_password_policy(new_password, email_normalized=account.email)
        new_hash = await _hash(new_password, self._params)
        now = self._now()
        async with self._db.session() as session:
            repo = AuthRepository(session)
            await repo.set_password_hash(
                user_id=user_id, password_hash=new_hash, now=now, changed=True
            )
            revoked = await repo.revoke_user_sessions(
                user_id=user_id, now=now, except_session_id=current_session_id
            )
        logger.info("auth_password_changed user_id=%s revoked_other=%d", user_id, revoked)
        return revoked
