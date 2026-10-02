"""Git history and structure ingestion (Phase 16, M4/M5, M26;
docs/PHASE16_ARCHITECTURE.md section 7).

    collector batch --validate--> git_commits (Postgres, idempotent by sha)
                    --project---> deterministic memories (Neo4j)

The same two-transaction shape as agent events: recording is Postgres-only and
idempotent, projection is retryable and idempotent by construction (deterministic
memory ids, existence checks, a row lock while projecting). Git facts are OBSERVED
evidence, so they skip duplicate/conflict classification (see `git_projection`); what
protects them is exact identity, deterministic ids and a `git_commit` source.

The server never reads the filesystem and never runs `git`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    ValidationError,
    is_infrastructure_error,
)
from app.db.engine import Database
from app.embeddings.base import EmbeddingClient
from app.models.enums import EntityType, SourceType
from app.models.project_history import ProjectionStatus
from app.projects.git_projection import (
    Draft,
    ancestors,
    branch_draft,
    build_commit_drafts,
    build_file_drafts,
    directory_draft,
    root_draft,
)
from app.projects.git_schemas import CommitIn, GitBatch, RepositoryIdentityIn, StructureBatch
from app.projects.identity import derive_identity
from app.repositories.memory_repository import MemoryRepository
from app.repositories.project_history_repository import GitCommitRepository
from app.repositories.project_repository import ProjectRecord
from app.schemas.memory import EntityInput, MemoryCreate, RelationshipInput, SourceInput
from app.semantic.embedding_pipeline import embed_and_store_memory
from app.services.memory_service import MemoryService
from app.services.project_service import ProjectScope, ProjectService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommitResult:
    sha: str
    status: str  # recorded | duplicate
    projection_status: ProjectionStatus
    note: str | None


@dataclass(frozen=True)
class StructureResult:
    files_received: int
    memories_created: int
    memories_existing: int
    memories_superseded: int
    files_stale: int


def _native(value: Any) -> datetime | None:  # noqa: ANN401
    if value is None:
        return None
    value = value.to_native() if hasattr(value, "to_native") else value
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class GitIngestService:
    def __init__(
        self,
        *,
        db: Database,
        graph: Any,  # noqa: ANN401 - GraphClient (or a test double)
        embeddings: EmbeddingClient,
        projects: ProjectService,
        settings: Settings,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_fn: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._db = db
        self._graph = graph
        self._embeddings = embeddings
        self._projects = projects
        self._settings = settings
        self._now = now_fn
        self._new_id = id_fn

    # -- guards --------------------------------------------------------------------

    async def _project(self, scope: ProjectScope) -> ProjectRecord:
        return await self._projects.get_owned(
            owner_user_id=scope.owner_user_id, project_id=scope.project_id
        )

    @staticmethod
    def _check_identity(project: ProjectRecord, identity: RepositoryIdentityIn) -> None:
        """Refuse a batch from a different repository than this project's (a guard
        against pushing repo B's history into project A by mistake)."""
        if project.identity_kind == "remote_url" and not identity.repository_url:
            raise ValidationError("identity.repository_url is required for this project")
        if project.identity_kind == "root_commits" and not identity.root_commits:
            raise ValidationError("identity.root_commits is required for this project")
        derived = derive_identity(
            repository_url=identity.repository_url
            if project.identity_kind == "remote_url"
            else None,
            root_commits=identity.root_commits if project.identity_kind == "root_commits" else None,
        )
        if derived.key != project.identity_key:
            raise ConflictError("This repository does not match the project's identity")

    # -- commits -------------------------------------------------------------------

    async def ingest_commits(self, scope: ProjectScope, batch: GitBatch) -> list[CommitResult]:
        project = await self._project(scope)
        self._check_identity(project, batch.identity)
        results: list[CommitResult] = []
        recorded: list[tuple[CommitIn, str, bool]] = []
        for commit in batch.commits:
            row_id, new = await self._record(scope, commit)
            recorded.append((commit, row_id, new))
        for commit, row_id, new in recorded:
            status, note = await self._project_safely(scope, project, row_id)
            results.append(
                CommitResult(commit.sha, "recorded" if new else "duplicate", status, note)
            )
        for branch in batch.branches:
            await self._project_branch_safely(scope, branch.name, branch.tip)
        return results

    async def _record(self, scope: ProjectScope, c: CommitIn) -> tuple[str, bool]:
        values = {
            "id": self._new_id(),
            "project_id": scope.project_id,
            "sha": c.sha,
            "parents": list(c.parents),
            "author_name": c.author_name,
            "author_email_sha256": c.author_email_sha256,
            "authored_at": c.authored_at,
            "committed_at": c.committed_at,
            "subject": c.subject,
            "body": c.body,
            "body_truncated": c.body_truncated,
            "branches": list(c.branches),
            "files": [f.model_dump(mode="json", exclude_none=True) for f in c.files],
            "files_changed_count": c.files_changed_count,
            "files_truncated": c.files_truncated,
            "additions": c.additions,
            "deletions": c.deletions,
            "recorded_at": self._now(),
        }
        async with self._db.session() as pg:
            row, inserted = await GitCommitRepository(pg, user_id=scope.space_id).insert_idempotent(
                values=values
            )
            if not inserted and (
                row.subject != c.subject
                or row.committed_at != c.committed_at
                or list(row.parents) != list(c.parents)
            ):
                # A hash names exactly one commit; a mismatch means a buggy or hostile client.
                raise ConflictError("commit was already recorded with different content",
                                    details={"sha": c.sha})  # fmt: skip
            return row.id, inserted

    async def _project_safely(
        self, scope: ProjectScope, project: ProjectRecord, commit_id: str
    ) -> tuple[ProjectionStatus, str | None]:
        try:
            return await self.project_commit(
                space_id=scope.space_id, project_name=project.name, commit_id=commit_id
            )
        except Exception as exc:  # noqa: BLE001 - recorded safely; never fail the request
            logger.warning(
                "git_projection_deferred commit_id=%s reason=%s", commit_id, type(exc).__name__
            )
            return "pending", None

    async def project_commit(
        self, *, space_id: str, project_name: str, commit_id: str
    ) -> tuple[ProjectionStatus, str | None]:
        async with self._db.session() as pg:
            repo = GitCommitRepository(pg, user_id=space_id)
            row = await repo.lock_for_projection(commit_id)
            if row is None:
                current = await repo.get(commit_id)
                if current is None:
                    raise NotFoundError("Commit not found")
                return current.projection_status, current.projection_error  # type: ignore[return-value]
            try:
                drafts = build_commit_drafts(
                    space_id=space_id,
                    commit_id=row.id,
                    sha=row.sha,
                    parents=list(row.parents),
                    subject=row.subject,
                    committed_at=row.committed_at,
                    branches=list(row.branches),
                    files=list(row.files),
                    files_changed_count=row.files_changed_count,
                    files_truncated=row.files_truncated,
                )
                created, existing = await self._write_drafts(
                    space_id,
                    row.id,
                    [root_draft(space_id, project_name), *drafts],
                    row.committed_at,
                )
                note = f"{created} memory(ies) created, {existing} already present"
                await repo.mark(
                    commit_id=row.id, status="projected", memory_id=drafts[0].memory_id, note=note
                )
                return "projected", note
            except AppError as exc:
                if not is_infrastructure_error(exc):
                    note = f"rejected: {exc.message}"
                    await repo.mark(commit_id=row.id, status="skipped", memory_id=None, note=note)
                    return "skipped", note
                exhausted = (
                    row.projection_attempts + 1 >= self._settings.project_projection_max_attempts
                )
                status = "failed" if exhausted else "pending"
                note = f"{type(exc).__name__}: retrying"
                await repo.mark(commit_id=row.id, status=status, memory_id=None, note=note)
                return status, note  # type: ignore[return-value]

    async def project_backlog(
        self, *, space_id: str, project_name: str, limit: int
    ) -> tuple[int, int]:
        async with self._db.session() as pg:
            ids = await GitCommitRepository(pg, user_id=space_id).unprojected(
                limit=limit, max_attempts=self._settings.project_projection_max_attempts
            )
        done = 0
        for commit_id in ids:
            try:
                status, _ = await self.project_commit(
                    space_id=space_id, project_name=project_name, commit_id=commit_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("git_backlog_failed reason=%s", type(exc).__name__)
                continue
            done += status in ("projected", "skipped")
        return len(ids), done

    async def _project_branch_safely(self, scope: ProjectScope, branch: str, tip: str) -> None:
        try:
            async with self._db.session() as pg:
                commit = await GitCommitRepository(pg, user_id=scope.space_id).by_sha(tip)
            if commit is None:
                return  # a tip outside the ingested range: nothing evidenced to record
            await self._write_drafts(
                scope.space_id, commit.id, [branch_draft(scope.space_id, branch, tip)],
                commit.committed_at,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            logger.warning("git_branch_deferred reason=%s", type(exc).__name__)

    # -- structure -----------------------------------------------------------------

    async def ingest_structure(self, scope: ProjectScope, batch: StructureBatch) -> StructureResult:
        project = await self._project(scope)
        self._check_identity(project, batch.identity)
        async with self._db.session() as pg:
            commit = await GitCommitRepository(pg, user_id=scope.space_id).by_sha(batch.commit_sha)
        if commit is None:
            raise NotFoundError("The snapshot commit has not been ingested for this project")
        space = scope.space_id
        files = [f.model_dump(mode="json") for f in batch.files]
        drafts: list[Draft] = [root_draft(space, project.name)]
        seen_dirs: set[str] = set()
        per_file: dict[str, list[Draft]] = {}
        for f in files:
            _, file_drafts = build_file_drafts(space_id=space, project_name=project.name, file=f)
            per_file[f["path"]] = file_drafts
            for d in ancestors(f["path"]):
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    drafts.append(directory_draft(space, project.name, d))
        all_file_drafts = [d for ds in per_file.values() for d in ds]
        created, existing, superseded, stale = await self._write_structure(
            space, commit.id, commit.committed_at, drafts, all_file_drafts, per_file
        )
        return StructureResult(len(files), created, existing, superseded, stale)

    async def _write_structure(  # noqa: PLR0913
        self,
        space: str,
        commit_id: str,
        at: datetime,
        plain: list[Draft],
        file_drafts: list[Draft],
        per_file: dict[str, list[Draft]],
    ) -> tuple[int, int, int, int]:
        repo = MemoryRepository(self._graph)
        present = await repo.existing_ids(
            user_id=space, ids=[d.memory_id for d in (*plain, *file_drafts)]
        )
        existing = len([d for d in plain if d.memory_id in present])
        created = superseded = stale = 0
        for d in plain:
            if d.memory_id not in present:
                await self._create(space, commit_id, d, at, [])
                created += 1
        for ds in per_file.values():
            first = ds[0]
            if first.memory_id in present:
                existing += len(ds)
                continue
            previous = await repo.active_memories_mentioning(
                user_id=space,
                normalized_name=first.supersede_entity.lower(),  # type: ignore[union-attr]
                prefix=first.supersede_prefix or "",
            )
            newer = [pid for pid, vf in previous if (_native(vf) or at) > at]
            if newer:
                stale += 1  # an older snapshot arrived after a newer one: never overwrite
                continue
            old_ids = [pid for pid, _ in previous]
            for index, d in enumerate(ds):
                if d.memory_id in present:
                    existing += 1
                    continue
                await self._create(space, commit_id, d, at, old_ids if index == 0 else [])
                created += 1
            superseded += len(old_ids)
        return created, existing, superseded, stale

    # -- shared --------------------------------------------------------------------

    async def _write_drafts(
        self, space: str, commit_id: str, drafts: list[Draft], at: datetime
    ) -> tuple[int, int]:
        repo = MemoryRepository(self._graph)
        present = await repo.existing_ids(user_id=space, ids=[d.memory_id for d in drafts])
        created = 0
        for d in drafts:
            if d.memory_id in present:
                continue
            old: list[str] = []
            if d.supersede_entity and d.supersede_prefix:
                previous = await repo.active_memories_mentioning(
                    user_id=space,
                    normalized_name=d.supersede_entity.lower(),
                    prefix=d.supersede_prefix,
                )
                if any((_native(vf) or at) > at for _, vf in previous):
                    continue  # an older fact arrived after a newer one
                old = [pid for pid, _ in previous]
            await self._create(space, commit_id, d, at, old)
            created += 1
        return created, len(drafts) - created

    async def _create(
        self, space: str, commit_id: str, d: Draft, at: datetime, supersedes: list[str]
    ) -> None:
        payload = MemoryCreate(
            content=d.content,
            confidence=1.0,
            entities=[EntityInput(name=n, entity_type=EntityType(t)) for n, t in d.entities],
            relationships=[
                RelationshipInput(source=a, predicate=p, target=b) for a, p, b in d.relationships
            ],
            source=SourceInput(
                source_type=SourceType.GIT_COMMIT, reference=commit_id, location=d.location
            ),
            valid_from=at,
            supersedes=supersedes,
        )
        service = MemoryService(MemoryRepository(self._graph), now_fn=self._now)
        await service.create(user_id=space, payload=payload, memory_id=d.memory_id)
        if d.embed:
            await embed_and_store_memory(
                user_id=space,
                memory_id=d.memory_id,
                content=d.content,
                db=self._db,
                embedding_client=self._embeddings,
                settings=self._settings,
                now_fn=self._now,
            )
