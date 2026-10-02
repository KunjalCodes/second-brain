"""Projects and project tokens (Phase 16, docs/PHASE16_ARCHITECTURE.md sections
3, 4, 9).

A project is a memory space. This service is the ONE place a `project_id`
becomes a `space_id`, and it does so only after an ownership check (account
route) or a token check (machine route). Nothing a client sends is ever used as
a space id directly.

Every method opens its own short transaction through `Database.session()`,
like `AuthService`, so authorisation state never rides on the protected
request's transaction.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.auth.tokens import (
    is_project_token,
    is_well_formed_project_token,
    new_project_token,
    token_digest,
)
from app.core.config import Settings
from app.core.errors import (
    AuthenticationRequiredError,
    ConflictError,
    NotFoundError,
)
from app.db.engine import Database
from app.projects.identity import derive_identity
from app.repositories.project_repository import (
    ProjectRecord,
    ProjectRepository,
    ProjectTokenRecord,
)

logger = logging.getLogger(__name__)

ALL_SCOPES = frozenset({"read", "ingest", "propose"})
#: Project ids reach SQL as UUIDs; anything else is a clean 404, never a cast error.
_NOT_FOUND = "Project not found"


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


@dataclass(frozen=True)
class ProjectScope:
    """The server-derived authority for one project request. `space_id` is the
    `users.id` every repository filters by; `owner_user_id` is the account that
    pays the rate limit and authorised the request."""

    project_id: str
    space_id: str
    owner_user_id: str
    scopes: frozenset[str]
    via: Literal["account", "project_token"]
    token_id: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        now_fn: Callable[[], datetime] = _now,
    ) -> None:
        self._db = db
        self._settings = settings
        self._now = now_fn

    # -- projects ------------------------------------------------------------------

    async def register(
        self,
        *,
        owner_user_id: str,
        name: str,
        repository_url: str | None,
        root_commits: list[str] | None,
        default_branch: str | None,
    ) -> tuple[ProjectRecord, bool]:
        """Idempotent: the same repository identity for the same account returns
        the existing project (created=False). Identity is validated before any
        write; a filesystem path is refused."""
        identity = derive_identity(repository_url=repository_url, root_commits=root_commits)
        now = self._now()
        async with self._db.session() as session:
            repo = ProjectRepository(session)
            existing = await repo.by_identity(owner_user_id, identity.key)
            if existing is not None:
                return existing, False
            if await repo.count_for_owner(owner_user_id) >= self._settings.projects_max_per_user:
                raise ConflictError(
                    "Project limit reached",
                    details={"limit": self._settings.projects_max_per_user},
                )
            project_id = str(uuid.uuid4())
            created = await repo.create(
                project_id=project_id,
                owner_user_id=owner_user_id,
                space_id=f"proj_{uuid.uuid4().hex}",
                name=name.strip(),
                identity_key=identity.key,
                identity_kind=identity.kind,
                default_branch=default_branch,
                now=now,
            )
            record = await repo.by_identity(owner_user_id, identity.key)
        assert record is not None
        if created:
            logger.info("project_registered owner=%s project_id=%s", owner_user_id, record.id)
        return record, created

    async def list_for_owner(
        self, *, owner_user_id: str, limit: int, offset: int
    ) -> list[ProjectRecord]:
        async with self._db.session() as session:
            return await ProjectRepository(session).list_for_owner(
                owner_user_id, limit=limit, offset=offset
            )

    async def get_owned(self, *, owner_user_id: str, project_id: str) -> ProjectRecord:
        if not _is_uuid(project_id):
            raise NotFoundError(_NOT_FOUND)
        async with self._db.session() as session:
            record = await ProjectRepository(session).owned(owner_user_id, project_id)
        if record is None:
            raise NotFoundError(_NOT_FOUND)  # another user's project looks identical
        return record

    async def scope_for_owner(self, *, owner_user_id: str, project_id: str) -> ProjectScope:
        record = await self.get_owned(owner_user_id=owner_user_id, project_id=project_id)
        if record.status != "active":
            raise NotFoundError(_NOT_FOUND)
        return ProjectScope(
            project_id=record.id,
            space_id=record.space_id,
            owner_user_id=owner_user_id,
            scopes=ALL_SCOPES,
            via="account",
        )

    async def resolve_space(self, *, owner_user_id: str, project_id: str) -> str:
        """`X-Memory-Space`: the space id for a project the account owns."""
        return (
            await self.scope_for_owner(owner_user_id=owner_user_id, project_id=project_id)
        ).space_id

    # -- tokens --------------------------------------------------------------------

    async def create_token(
        self,
        *,
        scope: ProjectScope,
        label: str,
        scopes: list[str],
        ttl_days: int | None,
    ) -> tuple[ProjectTokenRecord, str]:
        ttl = min(ttl_days or self._settings.project_token_default_ttl_days, 365)
        now = self._now()
        secret = new_project_token()
        async with self._db.session() as session:
            repo = ProjectRepository(session)
            active = await repo.active_token_count(scope.project_id, now=now)
            if active >= self._settings.project_tokens_max_per_project:
                raise ConflictError(
                    "Token limit reached for this project",
                    details={"limit": self._settings.project_tokens_max_per_project},
                )
            record = await repo.create_token(
                token_id=str(uuid.uuid4()),
                project_id=scope.project_id,
                token_sha256=token_digest(secret),
                label=label.strip(),
                scopes=sorted(set(scopes)),
                now=now,
                expires_at=now + timedelta(days=ttl),
            )
        logger.info(
            "project_token_created project_id=%s token_id=%s scopes=%s",
            scope.project_id,
            record.id,
            ",".join(record.scopes),
        )
        return record, secret

    async def list_tokens(
        self, *, scope: ProjectScope, limit: int = 50
    ) -> list[ProjectTokenRecord]:
        async with self._db.session() as session:
            return await ProjectRepository(session).list_tokens(scope.project_id, limit=limit)

    async def revoke_token(self, *, scope: ProjectScope, token_id: str) -> None:
        if not _is_uuid(token_id):
            raise NotFoundError("Token not found")
        async with self._db.session() as session:
            revoked = await ProjectRepository(session).revoke_token(
                project_id=scope.project_id, token_id=token_id, now=self._now()
            )
        if not revoked:
            raise NotFoundError("Token not found")
        logger.info("project_token_revoked project_id=%s token_id=%s", scope.project_id, token_id)

    async def authenticate_token(self, token: str) -> ProjectScope:
        """Resolve a presented `sbp_` token to its project scope, or raise the ONE
        generic `AuthenticationRequiredError`. The cause is logged (never the token)."""

        def reject(reason: str, token_id: str | None = None) -> AuthenticationRequiredError:
            logger.info("project_token_rejected reason=%s token_id=%s", reason, token_id)
            return AuthenticationRequiredError()

        if not token or not is_project_token(token) or not is_well_formed_project_token(token):
            raise reject("malformed")
        async with self._db.session() as session:
            found = await ProjectRepository(session).token_with_project(token_digest(token))
        if found is None:
            raise reject("unknown")
        record, project, owner_status = found
        now = self._now()
        if record.revoked_at is not None:
            raise reject("revoked", record.id)
        if now >= record.expires_at:
            raise reject("expired", record.id)
        if project.status != "active":
            raise reject("project_inactive", record.id)
        if owner_status is not None and owner_status != "active":
            raise reject("owner_disabled", record.id)
        if (
            record.last_used_at is None
            or (now - record.last_used_at).total_seconds()
            >= self._settings.auth_session_touch_seconds
        ):
            async with self._db.session() as session:
                await ProjectRepository(session).touch_token(token_id=record.id, now=now)
        return ProjectScope(
            project_id=project.id,
            space_id=project.space_id,
            owner_user_id=project.owner_user_id,
            scopes=frozenset(record.scopes),
            via="project_token",
            token_id=record.id,
        )
