# ruff: noqa: E501 - compact response tables
"""Deterministic project-history queries (Phase 16, M11-M14, M17, M18).

Every method is bounded, read-only, LLM-free and scoped to ONE project: the project's
memory-space id is the only isolation key, derived by the server from the credential.

What these answers are made of -- and what they are never made of:

* **Recorded facts only.** Events the agent sent, commits the collector sent, structure
  the parser read. Nothing is generated.
* **Labelled evidence.** Each item says whether it is `git`, `agent_stated` or
  `parser`; anything computed from those says `derived` and names its basis.
* **No inferred causes, failures or rationales.** A decision's reason is shown only if
  the agent stated it; "what changed afterwards" is an OBSERVED_OUTCOME with its basis
  named; a failed attempt exists only where an outcome was recorded.
* **Valid time and recorded time are separate fields** and ordering is by valid time.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from app.core.errors import NotFoundError, ValidationError
from app.db.engine import Database
from app.db.models import AgentEvent, AgentSession, GitCommit
from app.models.project_history import AgentEventType, event_text
from app.projects.cursor import decode_cursor, encode_cursor
from app.projects.git_projection import commit_part_ids, structure_prefix
from app.projects.naming import file_name, normalize_path, normalize_session_key
from app.repositories.memory_repository import MemoryRepository
from app.repositories.project_history_repository import (
    AgentEventRepository,
    AgentSessionRepository,
    GitCommitRepository,
)
from app.schemas.project_query import (
    AttemptsResponse,
    AttemptView,
    BugHistory,
    BugView,
    CoChangedFile,
    CommitContext,
    CommitRef,
    DecisionRef,
    DecisionsResponse,
    DecisionView,
    ErrorsResponse,
    ErrorView,
    FileHistory,
    FileStructureView,
    FixesResponse,
    HistoryResponse,
    ObservedOutcome,
    Rationale,
    RelatedChanges,
    SessionDetail,
    SessionList,
    SessionView,
    SourceRef,
    SourceView,
    TimelineItem,
)
from app.services.project_service import ProjectScope

MAX_LIMIT = 50
_LOAD_CAP = 500
_HEX = re.compile(r"^[0-9a-f]{7,64}$")


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _limit(value: int, default: int = 20) -> int:
    return max(1, min(value or default, MAX_LIMIT))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("date filters must include a timezone")
    return value.astimezone(UTC)


def _title(kind: AgentEventType, payload: dict) -> str:
    text = event_text(kind, payload) or kind.value
    return text.splitlines()[0][:300] if text else kind.value


_DETAIL_KEYS = (
    "outcome", "result", "fixes_error", "fixes_bug", "bug", "files", "feature", "approach",
    "change", "sha", "unfinished", "needs", "supersedes_decision", "command", "path",
)  # fmt: skip


def _event_item(event: AgentEvent, session_key: str) -> TimelineItem:
    kind = AgentEventType(event.event_type)
    details = {k: event.payload[k] for k in _DETAIL_KEYS if k in event.payload}
    return TimelineItem(
        kind="event",
        type=kind.value,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        title=_title(kind, event.payload),
        session_id=session_key,
        evidence="agent_stated",
        source=SourceRef(type="agent_event", id=event.id),
        memory_id=event.memory_id,
        details=details,
    )


def _file_entry(commit: GitCommit, path: str) -> dict | None:
    for f in commit.files:
        if f.get("path") == path or f.get("old_path") == path:
            return f
    return None


def _commit_ref(commit: GitCommit, path: str | None = None) -> CommitRef:
    entry = _file_entry(commit, path) if path else None
    return CommitRef(
        sha=commit.sha,
        subject=commit.subject,
        committed_at=commit.committed_at,
        status=entry.get("status") if entry else None,
        additions=entry.get("additions") if entry else None,
        deletions=entry.get("deletions") if entry else None,
        old_path=entry.get("old_path") if entry else None,
        source=SourceRef(type="git_commit", id=commit.id),
    )


def _commit_item(commit: GitCommit) -> TimelineItem:
    return TimelineItem(
        kind="commit",
        type="commit",
        occurred_at=commit.committed_at,
        recorded_at=commit.recorded_at,
        title=commit.subject,
        evidence="git",
        source=SourceRef(type="git_commit", id=commit.id),
        memory_id=commit.memory_id,
        details={
            "sha": commit.sha,
            "files_changed_count": commit.files_changed_count,
            "branches": list(commit.branches)[:5],
        },
    )


def _touches(payload: dict, files: set[str]) -> bool:
    if not files:
        return False
    named = {payload.get("path"), payload.get("old_path"), *payload.get("files", [])}
    return bool(files & {n for n in named if n})


class ProjectQueryService:
    def __init__(self, *, db: Database, graph: Any = None) -> None:  # noqa: ANN401
        self._db = db
        self._graph = graph

    # -- timeline ------------------------------------------------------------------

    async def history(
        self,
        scope: ProjectScope,
        *,
        topic: str | None = None,
        kinds: list[str] | None = None,
        types: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> HistoryResponse:
        limit = _limit(limit)
        since, until, before = _aware(since), _aware(until), decode_cursor(cursor)
        want = set(kinds or ["events", "commits"])
        if not want <= {"events", "commits"}:
            raise ValidationError("kinds must be 'events' and/or 'commits'")
        try:
            event_types = [AgentEventType(t).value for t in types] if types else None
        except ValueError:
            raise ValidationError("types must be agent event types") from None
        items: list[tuple[datetime, str, TimelineItem]] = []
        async with self._db.session() as pg:
            if "events" in want:
                rows = await AgentEventRepository(pg, user_id=scope.space_id).timeline(
                    project_id=scope.project_id, topic=topic, types=event_types, session_id=None,
                    since=since, until=until, before=before, limit=limit + 1,
                )  # fmt: skip
                items += [(e.occurred_at, e.id, _event_item(e, key)) for e, key in rows]
            if "commits" in want and not event_types:
                commits = await GitCommitRepository(pg, user_id=scope.space_id).timeline(
                    project_id=scope.project_id, topic=topic, since=since, until=until,
                    before=before, limit=limit + 1,
                )  # fmt: skip
                items += [(c.committed_at, c.id, _commit_item(c)) for c in commits]
        items.sort(key=lambda t: (t[0], t[1]), reverse=True)
        page = items[:limit]
        more = len(items) > limit
        next_cursor = encode_cursor(page[-1][0], page[-1][1]) if more and page else None
        return HistoryResponse(items=[i for _, _, i in page], next_cursor=next_cursor)

    # -- files ---------------------------------------------------------------------

    async def file_history(self, scope: ProjectScope, *, path: str, limit: int = 20) -> FileHistory:
        path = normalize_path(path)
        limit = _limit(limit)
        async with self._db.session() as pg:
            commits = await GitCommitRepository(pg, user_id=scope.space_id).touching_file(
                project_id=scope.project_id, path=path, limit=200
            )
            events = await AgentEventRepository(pg, user_id=scope.space_id).touching_file(
                project_id=scope.project_id, path=path, limit=200
            )
        refs = [_commit_ref(c, path) for c in commits]
        created = (
            next((r for r in reversed(refs) if r.status == "A"), None)
            if len(commits) < 200
            else None
        )
        by_type: dict[str, list[TimelineItem]] = defaultdict(list)
        sessions: dict[str, dict[str, Any]] = {}
        for event, key in events:
            item = _event_item(event, key)
            by_type[event.event_type].append(item)
            if event.event_type in ("file_inspected", "file_modified"):
                s = sessions.setdefault(
                    key,
                    {"session_id": key, "inspected": 0, "modified": 0,
                     "first_at": event.occurred_at, "last_at": event.occurred_at},
                )  # fmt: skip
                s["inspected" if event.event_type == "file_inspected" else "modified"] += 1
                s["first_at"] = min(s["first_at"], event.occurred_at)
                s["last_at"] = max(s["last_at"], event.occurred_at)
        return FileHistory(
            path=path,
            exists_at_last_change=(refs[0].status != "D") if refs and refs[0].status else None,
            created=created,
            last_changed=refs[0] if refs else None,
            commits=refs[:limit],
            sessions=sorted(sessions.values(), key=lambda s: s["last_at"], reverse=True)[:limit],
            decisions=by_type["decision"][:limit],
            bugs_and_errors=(by_type["bug_discovered"] + by_type["error"])[:limit],
            fixes=by_type["fix"][:limit],
            tests=by_type["test_run"][:limit],
            structure=await self._structure(scope, path),
            truncated=len(commits) >= 200 or len(events) >= 200,
        )

    async def _structure(self, scope: ProjectScope, path: str) -> FileStructureView | None:
        if self._graph is None:
            return None
        repo = MemoryRepository(self._graph)
        found = await repo.active_memories_mentioning(
            user_id=scope.space_id,
            normalized_name=file_name(path).lower(),
            prefix=structure_prefix(path),
            limit=10,
        )
        if not found:
            return None
        detail = await repo.get(user_id=scope.space_id, memory_id=found[0][0])
        if detail is None:
            return None
        fname = file_name(path).lower()
        by_id = {e.id: e for e in detail.entities}
        symbols: list[str] = []
        deps: list[str] = []
        for rel in detail.relationships:
            if by_id.get(rel.source_entity_id) and by_id[rel.source_entity_id].normalized_name == fname:
                target = by_id.get(rel.target_entity_id)
                if target is None:
                    continue
                if rel.predicate == "defines":
                    symbols.append(target.name.split("::", 1)[-1])
                elif rel.predicate == "depends_on":
                    deps.append(target.name)
        language = re.search(r"\(([^)]+)\):", detail.memory.content)
        return FileStructureView(
            language=language.group(1) if language else None,
            symbols=sorted(symbols)[:100],
            depends_on=sorted(deps)[:100],
            memory_id=detail.memory.id,
            as_of=detail.memory.valid_from,
            previous_versions=len(detail.supersedes),
        )

    async def commit_context(self, scope: ProjectScope, *, commit: str) -> CommitContext:
        prefix = commit.strip().lower()
        if not _HEX.match(prefix):
            raise ValidationError("commit must be 7-64 hex characters")
        async with self._db.session() as pg:
            repo = GitCommitRepository(pg, user_id=scope.space_id)
            found = await repo.by_sha_prefix(project_id=scope.project_id, prefix=prefix)
            if not found:
                raise NotFoundError("Commit not found")
            if len(found) > 1:
                raise ValidationError("commit prefix is ambiguous; use more characters")
            c = found[0]
            parents = [p for sha in c.parents if (p := await repo.by_sha(sha))]
            children = await repo.children_of(project_id=scope.project_id, sha=c.sha)
            events_repo = AgentEventRepository(pg, user_id=scope.space_id)
            commit_events = [
                (e, k)
                for e, k in await events_repo.timeline(
                    project_id=scope.project_id,
                    topic=c.sha,
                    types=["commit"],
                    session_id=None,
                    since=None,
                    until=None,
                    before=None,
                    limit=20,
                )  # fmt: skip
                if e.payload.get("sha") == c.sha
            ]
            context: list[TimelineItem] = []
            sessions: list[dict[str, Any]] = []
            for event, key in commit_events:
                sessions.append({"session_id": key, "event_id": event.id, "at": event.occurred_at})
                rows = await events_repo.timeline(
                    project_id=scope.project_id,
                    topic=None,
                    types=["decision", "bug_discovered", "error", "fix", "test_run"],
                    session_id=event.session_id, since=None, until=None, before=None, limit=15,
                )  # fmt: skip
                context += [_event_item(e, k) for e, k in rows]
        memory_ids = (
            commit_part_ids(scope.space_id, c.id, len(c.files))
            if c.projection_status == "projected"
            else []
        )
        return CommitContext(
            commit=_commit_ref(c),
            parents=[_commit_ref(p) for p in parents],
            children=[_commit_ref(x) for x in children],
            author_name=c.author_name,
            body=c.body,
            branches=list(c.branches),
            files=list(c.files)[:100],
            files_changed_count=c.files_changed_count,
            files_truncated=c.files_truncated,
            sessions=sessions,
            same_session_context=context[:30],
            memory_ids=memory_ids[:10],
        )

    async def related_changes(
        self, scope: ProjectScope, *, path: str, limit: int = 10
    ) -> RelatedChanges:
        path = normalize_path(path)
        limit = _limit(limit, 10)
        async with self._db.session() as pg:
            commits = await GitCommitRepository(pg, user_id=scope.space_id).touching_file(
                project_id=scope.project_id, path=path, limit=200
            )
        together: dict[str, list[datetime]] = defaultdict(list)
        for c in commits:
            for f in c.files:
                other = f.get("path")
                if other and other != path:
                    together[other].append(c.committed_at)
        ranked = sorted(together.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]
        return RelatedChanges(
            path=path,
            commits_examined=len(commits),
            cochanged=[
                CoChangedFile(path=p, commits_together=len(ts), last_together=max(ts))
                for p, ts in ranked
            ],
            recent_commits=[_commit_ref(c, path) for c in commits[:5]],
        )

    # -- decisions -----------------------------------------------------------------

    async def decisions(
        self,
        scope: ProjectScope,
        *,
        topic: str | None = None,
        file: str | None = None,
        include_superseded: bool = True,
        limit: int = 20,
        cursor: str | None = None,
    ) -> DecisionsResponse:
        limit = _limit(limit)
        file = normalize_path(file) if file else None
        before = decode_cursor(cursor)
        async with self._db.session() as pg:
            events_repo = AgentEventRepository(pg, user_id=scope.space_id)
            rows = await events_repo.timeline(
                project_id=scope.project_id, topic=None, types=["decision"], session_id=None,
                since=None, until=None, before=None, limit=_LOAD_CAP + 1,
            )  # fmt: skip
            truncated = len(rows) > _LOAD_CAP
            rows = rows[:_LOAD_CAP]
            ordered = sorted(rows, key=lambda r: (r[0].occurred_at, r[0].id))  # oldest first
            by_title: dict[str, list[AgentEvent]] = defaultdict(list)
            for e, _ in ordered:
                by_title[_norm(e.payload["title"])].append(e)

            def supersedes_of(e: AgentEvent) -> AgentEvent | None:
                old = e.payload.get("supersedes_decision")
                prior = [
                    p
                    for p in by_title.get(_norm(old or ""), [])
                    if p.occurred_at <= e.occurred_at and p.id != e.id
                ]
                return prior[-1] if prior else None

            def superseded_by_of(e: AgentEvent) -> AgentEvent | None:
                mine = _norm(e.payload["title"])
                later = [
                    x for x, _ in ordered
                    if x.id != e.id and x.occurred_at >= e.occurred_at
                    and _norm(x.payload.get("supersedes_decision") or "") == mine
                ]  # fmt: skip
                return later[0] if later else None

            wanted = []
            needle = _norm(topic) if topic else None
            for e, key in reversed(ordered):  # newest first
                p = e.payload
                if file and file not in p.get("files", []):
                    continue
                if needle and needle not in _norm(
                    " ".join(
                        [
                            p["title"],
                            p.get("rationale", ""),
                            p.get("feature", ""),
                            *p.get("files", []),
                        ]
                    )
                ):
                    continue
                newer = superseded_by_of(e)
                if newer and not include_superseded:
                    continue
                if before and not ((e.occurred_at, e.id) < before):
                    continue
                wanted.append((e, key, newer))
            page = wanted[:limit]
            views: list[DecisionView] = []
            for e, key, newer in page:
                older = supersedes_of(e)
                views.append(
                    DecisionView(
                        id=e.id,
                        title=e.payload["title"],
                        status="superseded" if newer else "active",
                        session_id=key,
                        occurred_at=e.occurred_at,
                        recorded_at=e.recorded_at,
                        rationale=Rationale(text=e.payload["rationale"]) if e.payload.get("rationale") else None,
                        alternatives=e.payload.get("alternatives", []),
                        files=e.payload.get("files", []),
                        feature=e.payload.get("feature"),
                        supersedes=DecisionRef(id=older.id, title=older.payload["title"], occurred_at=older.occurred_at) if older else None,
                        superseded_by=DecisionRef(id=newer.id, title=newer.payload["title"], occurred_at=newer.occurred_at) if newer else None,
                        observed_outcomes=await self._outcomes(events_repo, e),
                        source=SourceRef(type="agent_event", id=e.id),
                        memory_id=e.memory_id,
                    )
                )  # fmt: skip
        next_cursor = None
        if len(wanted) > limit:
            last = page[-1][0]
            next_cursor = encode_cursor(last.occurred_at, last.id)
        return DecisionsResponse(decisions=views, next_cursor=next_cursor, truncated=truncated)

    async def _outcomes(
        self, events_repo: AgentEventRepository, decision: AgentEvent
    ) -> list[ObservedOutcome]:
        named = set(decision.payload.get("files", []))
        if not named:
            return []  # nothing to observe against; never guess
        later = await events_repo.of_session(
            session_id=decision.session_id, after_seq=decision.recorded_seq, limit=200
        )
        out = []
        for e in later:
            if e.event_type in ("file_modified", "commit", "test_run") and _touches(
                e.payload, named
            ):
                kind = AgentEventType(e.event_type)
                out.append(
                    ObservedOutcome(
                        type=kind.value,
                        occurred_at=e.occurred_at,
                        title=_title(kind, e.payload),
                        source=SourceRef(type="agent_event", id=e.id),
                    )
                )
            if len(out) >= 5:
                break
        return out

    # -- bugs, errors, fixes, attempts ---------------------------------------------

    async def _load(
        self, scope: ProjectScope, types: list[str]
    ) -> tuple[list[tuple[AgentEvent, str]], bool]:
        async with self._db.session() as pg:
            rows = await AgentEventRepository(pg, user_id=scope.space_id).timeline(
                project_id=scope.project_id, topic=None, types=types, session_id=None,
                since=None, until=None, before=None, limit=_LOAD_CAP + 1,
            )  # fmt: skip
        truncated = len(rows) > _LOAD_CAP
        return sorted(rows[:_LOAD_CAP], key=lambda r: (r[0].occurred_at, r[0].id)), truncated

    async def bug_history(
        self, scope: ProjectScope, *, topic: str | None = None, limit: int = 20
    ) -> BugHistory:
        limit = _limit(limit)
        rows, truncated = await self._load(scope, ["bug_discovered", "error", "fix", "test_run"])
        bugs: dict[str, dict[str, Any]] = {}

        def bug(title: str) -> dict[str, Any]:
            return bugs.setdefault(
                _norm(title),
                {"title": title, "discovered": [], "errors": [], "fixes": [], "tests": []},
            )

        error_bug: dict[str, str] = {}
        for e, key in rows:
            p = e.payload
            if e.event_type == "bug_discovered":
                bug(p["title"])["discovered"].append((e, key))
            elif e.event_type == "error" and p.get("bug"):
                bug(p["bug"])["errors"].append((e, key))
                error_bug[_norm(p.get("title") or p["message"].splitlines()[0][:160])] = p["bug"]
        for e, key in rows:
            p = e.payload
            if e.event_type == "fix":
                target = p.get("fixes_bug") or error_bug.get(_norm(p.get("fixes_error") or ""))
                if target:
                    bug(target)["fixes"].append((e, key))
        fix_to_bug = {_norm(f.payload["title"]): b for b, v in bugs.items() for f, _ in v["fixes"]}
        for e, key in rows:
            if e.event_type == "test_run" and e.payload["result"] == "passed":
                for t in e.payload.get("validates", []):
                    if _norm(t) in fix_to_bug:
                        bugs[fix_to_bug[_norm(t)]]["tests"].append((e, key))
        needle = _norm(topic) if topic else None
        views = []
        for v in bugs.values():
            if needle and needle not in _norm(
                v["title"]
                + " "
                + " ".join(e.payload.get("description", "") for e, _ in v["discovered"])
            ):
                continue
            status, basis = self._bug_status(v)
            last = max(
                (e.occurred_at for k in ("discovered", "errors", "fixes") for e, _ in v[k]),
                default=datetime.min.replace(tzinfo=UTC),
            )
            views.append((last, BugView(
                title=v["title"], status=status, status_basis=basis,
                discovered=[_event_item(e, k) for e, k in v["discovered"]],
                errors=[_event_item(e, k) for e, k in v["errors"]][-10:],
                fixes=[_event_item(e, k) for e, k in v["fixes"]],
                validated_by=[_event_item(e, k) for e, k in v["tests"]],
            )))  # fmt: skip
        views.sort(key=lambda t: t[0], reverse=True)
        return BugHistory(
            bugs=[b for _, b in views[:limit]], truncated=truncated or len(views) > limit
        )

    @staticmethod
    def _bug_status(v: dict[str, Any]) -> tuple[str, str]:
        fixes = sorted(v["fixes"], key=lambda r: (r[0].occurred_at, r[0].id))
        if not fixes:
            return "open", "no fix event has been recorded for this bug"
        newest = fixes[-1][0]
        outcome = newest.payload["outcome"]
        later_error = any(e.occurred_at > newest.occurred_at for e, _ in v["errors"])
        if outcome == "succeeded" and later_error:
            return (
                "error_recorded_after_fix",
                "an error naming this bug was recorded after its newest succeeded fix",
            )
        mapping = {
            "succeeded": "fixed",
            "partial": "partially_fixed",
            "failed": "fix_attempted_failed",
        }
        return mapping[outcome], f"the newest fix event states outcome '{outcome}'"  # type: ignore[return-value]

    async def related_errors(
        self,
        scope: ProjectScope,
        *,
        topic: str | None = None,
        file: str | None = None,
        limit: int = 20,
    ) -> ErrorsResponse:
        limit = _limit(limit)
        file = normalize_path(file) if file else None
        rows, truncated = await self._load(scope, ["error", "fix"])
        fixes_by_error: dict[str, list[tuple[AgentEvent, str]]] = defaultdict(list)
        for e, key in rows:
            if e.event_type == "fix" and e.payload.get("fixes_error"):
                fixes_by_error[_norm(e.payload["fixes_error"])].append((e, key))
        groups: dict[str, list[tuple[AgentEvent, str]]] = defaultdict(list)
        for e, key in rows:
            if e.event_type == "error":
                groups[
                    _norm(e.payload.get("title") or e.payload["message"].splitlines()[0][:160])
                ].append((e, key))
        needle = _norm(topic) if topic else None
        out = []
        for norm_title, events in groups.items():
            files = sorted({f for e, _ in events for f in e.payload.get("files", [])})
            text = " ".join(e.payload["message"] for e, _ in events) + " " + norm_title
            if file and file not in files:
                continue
            if needle and needle not in _norm(text):
                continue
            first, last = events[0][0], events[-1][0]
            out.append(ErrorView(
                title=last.payload.get("title") or last.payload["message"].splitlines()[0][:160],
                occurrences=len(events), first_seen=first.occurred_at, last_seen=last.occurred_at,
                bug=next((e.payload["bug"] for e, _ in reversed(events) if e.payload.get("bug")), None),
                files=files[:10],
                fixes=[_event_item(e, k) for e, k in fixes_by_error.get(norm_title, [])],
                events=[_event_item(e, k) for e, k in events][-5:],
            ))  # fmt: skip
        out.sort(key=lambda v: v.last_seen, reverse=True)
        return ErrorsResponse(errors=out[:limit], truncated=truncated or len(out) > limit)

    async def fix_history(
        self,
        scope: ProjectScope,
        *,
        topic: str | None = None,
        outcome: str | None = None,
        limit: int = 20,
    ) -> FixesResponse:
        limit = _limit(limit)
        if outcome and outcome not in ("succeeded", "partial", "failed"):
            raise ValidationError("outcome must be succeeded, partial or failed")
        rows, truncated = await self._load(scope, ["fix"])
        needle = _norm(topic) if topic else None
        picked = [
            (e, k) for e, k in reversed(rows)
            if (not outcome or e.payload["outcome"] == outcome)
            and (not needle or needle in _norm(" ".join(str(e.payload.get(f, "")) for f in ("title", "description", "fixes_error", "fixes_bug"))))
        ]  # fmt: skip
        return FixesResponse(
            fixes=[_event_item(e, k) for e, k in picked[:limit]],
            truncated=truncated or len(picked) > limit,
        )

    async def attempts(
        self, scope: ProjectScope, *, subject: str | None = None, limit: int = 20
    ) -> AttemptsResponse:
        """'What did I already try?' Groups events by the agent's own approach label (or a
        fix's title). The outcome is read from explicit fields only."""
        limit = _limit(limit)
        rows, truncated = await self._load(scope, ["error", "fix", "test_run", "task_blocked"])
        groups: dict[str, list[tuple[AgentEvent, str]]] = defaultdict(list)
        labels: dict[str, str] = {}
        for e, key in rows:
            p = e.payload
            label = p.get("approach") or (p["title"] if e.event_type == "fix" else None)
            if label:
                groups[_norm(label)].append((e, key))
                labels.setdefault(_norm(label), label)
        needle = _norm(subject) if subject else None
        views = []
        for norm_label, events in groups.items():
            text = " ".join(
                " ".join(
                    str(e.payload.get(f, ""))
                    for f in (
                        "title",
                        "message",
                        "bug",
                        "fixes_bug",
                        "fixes_error",
                        "feature",
                        "reason",
                        "command",
                        "description",
                    )
                )
                for e, _ in events
            )
            if needle and needle not in _norm(labels[norm_label] + " " + text):
                continue
            outcome, basis = self._attempt_outcome(events)
            views.append(AttemptView(
                approach=labels[norm_label], first_at=events[0][0].occurred_at, last_at=events[-1][0].occurred_at,
                outcome=outcome, outcome_basis=basis,  # type: ignore[arg-type]
                errors=sorted({(e.payload.get("title") or e.payload["message"].splitlines()[0])[:160] for e, _ in events if e.event_type == "error"}),
                bugs=sorted({b for e, _ in events for b in (e.payload.get("bug"), e.payload.get("fixes_bug")) if b}),
                events=[_event_item(e, k) for e, k in events][:20],
            ))  # fmt: skip
        views.sort(key=lambda v: v.first_at)
        return AttemptsResponse(attempts=views[:limit], truncated=truncated or len(views) > limit)

    @staticmethod
    def _attempt_outcome(events: list[tuple[AgentEvent, str]]) -> tuple[str, str]:
        fixes = [e for e, _ in events if e.event_type == "fix"]
        if fixes:
            return fixes[-1].payload["outcome"], "the newest fix event in this attempt states it"
        tests = [e for e, _ in events if e.event_type == "test_run"]
        if tests:
            res = tests[-1].payload["result"]
            return (
                "tests_passed" if res == "passed" else "tests_failed"
            ), "the newest test run in this attempt"
        if any(e.event_type == "task_blocked" for e, _ in events):
            return "blocked", "a TASK_BLOCKED event carried this approach"
        return (
            "no_outcome_recorded",
            "no fix, test or blocked event states an outcome for this approach",
        )

    # -- sessions ------------------------------------------------------------------

    @staticmethod
    def _session_view(s: AgentSession) -> SessionView:
        return SessionView(
            session_id=s.session_key, agent_name=s.agent_name, status=s.status,  # type: ignore[arg-type]
            started_at=s.started_at, ended_at=s.ended_at, last_event_at=s.last_event_at,
            outcome=s.outcome, summary=s.summary, event_count=s.event_count,  # type: ignore[arg-type]
        )  # fmt: skip

    async def sessions(
        self, scope: ProjectScope, *, limit: int = 20, cursor: str | None = None
    ) -> SessionList:
        limit = _limit(limit)
        before = decode_cursor(cursor)
        async with self._db.session() as pg:
            rows = await AgentSessionRepository(pg, user_id=scope.space_id).recent(
                project_id=scope.project_id, limit=limit + 1, before=before
            )
        page = rows[:limit]
        nxt = (
            encode_cursor(page[-1].last_event_at, page[-1].id)
            if len(rows) > limit and page
            else None
        )
        return SessionList(sessions=[self._session_view(s) for s in page], next_cursor=nxt)

    async def session(
        self, scope: ProjectScope, *, session_id: str, after_seq: int = 0, limit: int = 100
    ) -> SessionDetail:
        limit = max(1, min(limit, 200))
        try:
            session_id = normalize_session_key(session_id)
        except ValidationError:
            raise NotFoundError("Session not found") from None  # not a possible key
        async with self._db.session() as pg:
            sessions = AgentSessionRepository(pg, user_id=scope.space_id)
            row = await sessions.by_key(session_id)
            if row is None or row.project_id != scope.project_id:
                raise NotFoundError("Session not found")
            events_repo = AgentEventRepository(pg, user_id=scope.space_id)
            events = await events_repo.of_session(
                session_id=row.id, after_seq=max(after_seq, 0), limit=limit + 1
            )
            counts = await events_repo.counts_by_type(session_id=row.id)
            everything = await events_repo.of_session(session_id=row.id, after_seq=0, limit=2000)
        page = events[:limit]
        view = self._session_view(row)

        def titles(kind: str, key: str = "title") -> list[str]:
            seen: dict[str, None] = {}
            for e in everything:
                if e.event_type == kind and e.payload.get(key):
                    seen[str(e.payload[key]).splitlines()[0][:200]] = None
            return list(seen)[:50]

        started = next((e for e in everything if e.event_type == "session_started"), None)
        ended = next((e for e in reversed(everything) if e.event_type == "session_ended"), None)
        return SessionDetail(
            session=view,
            goal=(started.payload.get("goal") if started else None),
            continues_session=(started.payload.get("continues_session") if started else None),
            counts=counts,
            requests=titles("user_request", "text"),
            files_inspected=titles("file_inspected", "path"),
            files_modified=titles("file_modified", "path"),
            decisions=titles("decision"),
            errors=titles("error", "message"),
            fixes=titles("fix"),
            tests=titles("test_run", "command"),
            commits=titles("commit", "sha"),
            unfinished=list(ended.payload.get("unfinished", [])) if ended else [],
            events=[_event_item(e, session_id) for e in page],
            next_after_seq=page[-1].recorded_seq if len(events) > limit and page else None,
        )

    # -- sources -------------------------------------------------------------------

    async def source(
        self, scope: ProjectScope, *, event_id: str | None = None, commit: str | None = None
    ) -> SourceView:
        if bool(event_id) == bool(commit):
            raise ValidationError("give exactly one of event_id or commit")
        async with self._db.session() as pg:
            if event_id:
                if not re.fullmatch(r"[0-9a-fA-F-]{36}", event_id):
                    raise NotFoundError("Event not found")
                found = await AgentEventRepository(pg, user_id=scope.space_id).by_id(
                    event_id.lower()
                )
                if found is None or found[0].project_id != scope.project_id:
                    raise NotFoundError("Event not found")
                e, key = found
                return SourceView(
                    kind="agent_event",
                    data={
                        "id": e.id, "type": e.event_type, "session_id": key, "occurred_at": e.occurred_at,
                        "recorded_at": e.recorded_at, "source": e.source, "payload": e.payload,
                        "projection_status": e.projection_status, "memory_id": e.memory_id,
                    },
                )  # fmt: skip
            prefix = commit.strip().lower()  # type: ignore[union-attr]
            if not _HEX.match(prefix):
                raise ValidationError("commit must be 7-64 hex characters")
            rows = await GitCommitRepository(pg, user_id=scope.space_id).by_sha_prefix(
                project_id=scope.project_id, prefix=prefix
            )
            if len(rows) != 1:
                raise (
                    NotFoundError("Commit not found")
                    if not rows
                    else ValidationError("commit prefix is ambiguous")
                )
            c = rows[0]
            return SourceView(
                kind="git_commit",
                data={
                    "id": c.id, "sha": c.sha, "parents": list(c.parents), "subject": c.subject, "body": c.body,
                    "author_name": c.author_name, "committed_at": c.committed_at, "branches": list(c.branches),
                    "files": list(c.files)[:200], "files_changed_count": c.files_changed_count,
                    "files_truncated": c.files_truncated, "additions": c.additions, "deletions": c.deletions,
                },
            )  # fmt: skip
