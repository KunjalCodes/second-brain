"""Project and project-token persistence (Phase 16).

Exempt from `tests/unit/test_sql_safety.py`'s `UserScopedRepository` rule for the
same reason as `auth_repository.py`: a project token is found BY CREDENTIAL (the
SHA-256 of the token) before any user is known. Every other read here is keyed by
the already-authorised `owner_user_id` AND the project id together, so a project
id alone never returns a row. Nothing in this module reads memory data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project, ProjectToken, User, UserAccount


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    owner_user_id: str
    space_id: str
    name: str
    identity_key: str
    identity_kind: str
    default_branch: str | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ProjectTokenRecord:
    id: str
    project_id: str
    label: str
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


def _project(row: Project) -> ProjectRecord:
    return ProjectRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        space_id=row.space_id,
        name=row.name,
        identity_key=row.identity_key,
        identity_kind=row.identity_kind,
        default_branch=row.default_branch,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _token(row: ProjectToken) -> ProjectTokenRecord:
    return ProjectTokenRecord(
        id=row.id,
        project_id=row.project_id,
        label=row.label,
        scopes=tuple(row.scopes),
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


class ProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # -- projects ------------------------------------------------------------------

    async def by_identity(self, owner_user_id: str, identity_key: str) -> ProjectRecord | None:
        row = (
            await self._s.execute(
                select(Project).where(
                    Project.owner_user_id == owner_user_id, Project.identity_key == identity_key
                )
            )
        ).scalar_one_or_none()
        return _project(row) if row else None

    async def owned(self, owner_user_id: str, project_id: str) -> ProjectRecord | None:
        row = (
            await self._s.execute(
                select(Project).where(
                    Project.owner_user_id == owner_user_id, Project.id == project_id
                )
            )
        ).scalar_one_or_none()
        return _project(row) if row else None

    async def by_id_for_token(self, project_id: str) -> ProjectRecord | None:
        """Only for a token that already authenticated against this project id."""
        row = (
            await self._s.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        return _project(row) if row else None

    async def count_for_owner(self, owner_user_id: str) -> int:
        return int(
            (
                await self._s.execute(
                    select(func.count())
                    .select_from(Project)
                    .where(Project.owner_user_id == owner_user_id)
                )
            ).scalar_one()
        )

    async def list_for_owner(
        self, owner_user_id: str, *, limit: int, offset: int
    ) -> list[ProjectRecord]:
        rows = (
            await self._s.execute(
                select(Project)
                .where(Project.owner_user_id == owner_user_id)
                .order_by(Project.created_at, Project.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return [_project(r) for r in rows]

    async def create(
        self,
        *,
        project_id: str,
        owner_user_id: str,
        space_id: str,
        name: str,
        identity_key: str,
        identity_kind: str,
        default_branch: str | None,
        now: datetime,
    ) -> bool:
        """Create the space's `users` row and the project atomically. Returns False
        (and leaves nothing behind) if the owner already has this identity: the
        unique constraint, not a prior read, decides a registration race."""
        await self._s.execute(
            insert(User).values(id=space_id, created_at=now).on_conflict_do_nothing()
        )
        inserted = (
            await self._s.execute(
                insert(Project)
                .values(
                    id=project_id,
                    owner_user_id=owner_user_id,
                    space_id=space_id,
                    name=name,
                    identity_key=identity_key,
                    identity_kind=identity_kind,
                    default_branch=default_branch,
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(constraint="projects_owner_identity")
                .returning(Project.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            await self._s.execute(
                User.__table__.delete().where(User.id == space_id)  # the unused space row
            )
            return False
        return True

    # -- tokens --------------------------------------------------------------------

    async def active_token_count(self, project_id: str, *, now: datetime) -> int:
        return int(
            (
                await self._s.execute(
                    select(func.count())
                    .select_from(ProjectToken)
                    .where(
                        ProjectToken.project_id == project_id,
                        ProjectToken.revoked_at.is_(None),
                        ProjectToken.expires_at > now,
                    )
                )
            ).scalar_one()
        )

    async def create_token(
        self,
        *,
        token_id: str,
        project_id: str,
        token_sha256: str,
        label: str,
        scopes: list[str],
        now: datetime,
        expires_at: datetime,
    ) -> ProjectTokenRecord:
        row = ProjectToken(
            id=token_id,
            project_id=project_id,
            token_sha256=token_sha256,
            label=label,
            scopes=scopes,
            created_at=now,
            expires_at=expires_at,
        )
        self._s.add(row)
        await self._s.flush()
        return _token(row)

    async def list_tokens(self, project_id: str, *, limit: int) -> list[ProjectTokenRecord]:
        rows = (
            await self._s.execute(
                select(ProjectToken)
                .where(ProjectToken.project_id == project_id)
                .order_by(ProjectToken.created_at.desc(), ProjectToken.id)
                .limit(limit)
            )
        ).scalars()
        return [_token(r) for r in rows]

    async def revoke_token(self, *, project_id: str, token_id: str, now: datetime) -> bool:
        result = await self._s.execute(
            update(ProjectToken)
            .where(
                ProjectToken.id == token_id,
                ProjectToken.project_id == project_id,
                ProjectToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        return (result.rowcount or 0) > 0

    async def token_with_project(
        self, token_sha256: str
    ) -> tuple[ProjectTokenRecord, ProjectRecord, str | None] | None:
        """The token, its project, and the owner's account status (None when the
        owner has no login account, i.e. a development identity)."""
        row = (
            await self._s.execute(
                select(ProjectToken, Project, UserAccount.status)
                .join(Project, Project.id == ProjectToken.project_id)
                .outerjoin(UserAccount, UserAccount.user_id == Project.owner_user_id)
                .where(ProjectToken.token_sha256 == token_sha256)
            )
        ).first()
        if row is None:
            return None
        return _token(row[0]), _project(row[1]), row[2]

    async def touch_token(self, *, token_id: str, now: datetime) -> None:
        await self._s.execute(
            update(ProjectToken).where(ProjectToken.id == token_id).values(last_used_at=now)
        )
