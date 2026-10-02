# ruff: noqa: E501 - compact assembly tables
"""`get_resume_context` (Phase 16, M15-M17): the smallest useful context needed to
continue earlier work.

This is NOT a memory search. It is a dedicated, deterministic continuity operation
over what was explicitly recorded:

    WHERE I LEFT OFF -> WHAT I WAS DOING -> WHAT I INVESTIGATED -> WHAT I DECIDED (and
    why, if stated) -> WHAT I CHANGED -> WHAT FAILED -> WHAT WAS FIXED -> WHAT WAS TESTED
    -> WHAT REMAINS

Hard rules:

* **Unfinished work is only what was explicitly marked**: an `unfinished` entry on
  SESSION_ENDED, a TASK_BLOCKED event, or a USER_REQUEST that no later TASK_COMPLETED /
  TASK_BLOCKED closed. It is never read from graph structure or file changes.
* **Failed attempts are only recorded outcomes** (a fix's stated outcome, a test result,
  a blocked task); see `ProjectQueryService.attempts`.
* **Reasons are shown only if the agent stated them.**
* **Bounded**: every section has a cap (lowerable by the caller, never raisable); text is
  trimmed; the graph is never dumped. `limits` reports what was cut.
* **Ranked by a documented heuristic**, in priority order: active unfinished work, most
  recent relevant session, recent decisions, unresolved bugs, recent errors, recent file
  changes, relevant commits, supporting memories. Within a section: status, then
  relevance reasons (heuristic), then recency.
* No LLM. Works with none configured.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.core.errors import AppError, NotFoundError, ValidationError
from app.db.engine import Database
from app.db.models import AgentEvent, AgentSession
from app.projects.naming import normalize_session_key
from app.repositories.project_history_repository import (
    AgentEventRepository,
    AgentSessionRepository,
    GitCommitRepository,
)
from app.schemas.project_query import (
    ActiveTask,
    AttemptBrief,
    BugBrief,
    DecisionBrief,
    ErrorBrief,
    MemoryBrief,
    ResumeContext,
    SectionLimit,
    SessionBrief,
    SourceRef,
    TestBrief,
    UnfinishedItem,
)
from app.services.project_query_service import ProjectQueryService, _commit_ref
from app.services.project_service import ProjectScope

#: Default and maximum items per section. A caller may lower these, never raise them.
SECTION_CAPS = {
    "unfinished_work": 10, "recent_decisions": 5, "files_inspected": 15, "files_modified": 15,
    "recent_commits": 5, "known_bugs": 5, "previous_errors": 5, "attempts": 5, "tests_run": 5,
    "important_memories": 5, "relevant_sources": 20,
}  # fmt: skip
RANKING = [
    "unfinished_work", "active_task", "last_session", "recent_decisions", "known_bugs",
    "previous_errors", "files_modified", "files_inspected", "recent_commits", "attempts",
    "tests_run", "important_memories",
]  # fmt: skip
_CHAIN_DEPTH = 3
_TRIM = 300
_STOP = frozenset(
    ["a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "from", "is", "are", "was", "were", "be", "this", "that", "it", "we", "our", "i", "you", "continue", "where", "left", "off", "work", "task"]
)


def _task_words(text: str | None) -> frozenset[str]:
    """Lower-cased words of a task description, minus filler, for the relevance bias."""
    return frozenset(w for w in re.findall(r"[a-z0-9_]{3,}", (text or "").lower()) if w not in _STOP)


_UNRESOLVED = {"open", "fix_attempted_failed", "partially_fixed", "error_recorded_after_fix"}


def _trim(text: str | None, n: int = _TRIM) -> str | None:
    if text is None:
        return None
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class ProjectResumeService:
    def __init__(
        self,
        *,
        db: Database,
        queries: ProjectQueryService,
        retrieval: Any = None,  # noqa: ANN401 - HybridRetrievalService (optional)
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._db = db
        self._q = queries
        self._retrieval = retrieval
        self._now = now_fn

    async def resume(
        self,
        scope: ProjectScope,
        *,
        project_name: str,
        session_id: str | None = None,
        task: str | None = None,
        max_items: int | None = None,
        include_unrelated: bool = False,
    ) -> ResumeContext:
        caps = {k: min(v, max_items) if max_items else v for k, v in SECTION_CAPS.items()}
        caps = {k: max(1, v) for k, v in caps.items()}
        limits: dict[str, SectionLimit] = {}

        def cut(name: str, items: list) -> list:
            limits[name] = SectionLimit(returned=min(len(items), caps[name]), available=len(items))
            return items[: caps[name]]

        async with self._db.session() as pg:
            sessions = AgentSessionRepository(pg, user_id=scope.space_id)
            recent = await sessions.recent(project_id=scope.project_id, limit=50)
            if session_id:
                try:
                    session_id = normalize_session_key(session_id)
                except ValidationError:
                    raise NotFoundError("Session not found") from None
                chosen = next(
                    (s for s in recent if s.session_key == session_id), None
                ) or await sessions.by_key(session_id)
                if chosen is None or chosen.project_id != scope.project_id:
                    raise NotFoundError("Session not found")
                last = chosen
            else:
                last = recent[0] if recent else None
            if last is None:
                return self._empty(scope, project_name, limits)
            by_key = {s.session_key: s for s in recent}
            chain = [last]
            events_repo = AgentEventRepository(pg, user_id=scope.space_id)
            chain_events: dict[str, list[AgentEvent]] = {}
            while True:
                chain_events[chain[-1].id] = await events_repo.of_session(
                    session_id=chain[-1].id, after_seq=0, limit=2000
                )
                started = next(
                    (e for e in chain_events[chain[-1].id] if e.event_type == "session_started"),
                    None,
                )
                previous_key = started.payload.get("continues_session") if started else None
                previous = by_key.get(previous_key) if previous_key else None
                if previous is None and previous_key:
                    previous = await sessions.by_key(previous_key)
                if (
                    previous is None
                    or len(chain) >= _CHAIN_DEPTH
                    or previous.id in {c.id for c in chain}
                ):
                    break
                chain.append(previous)
            recent_commits = await GitCommitRepository(pg, user_id=scope.space_id).recent(
                project_id=scope.project_id, limit=caps["recent_commits"] + 1
            )
            test_rows = await events_repo.timeline(
                project_id=scope.project_id, topic=None, types=["test_run"], session_id=None,
                since=None, until=None, before=None, limit=caps["tests_run"] + 1,
            )  # fmt: skip

        now = self._now()
        chain_ids = {s.id for s in chain}
        all_events = [e for s in chain for e in chain_events[s.id]]
        modified = self._unique(
            [
                e.payload["path"]
                for e in sorted(all_events, key=lambda e: e.occurred_at, reverse=True)
                if e.event_type == "file_modified"
            ]
        )
        inspected = self._unique(
            [
                e.payload["path"]
                for e in sorted(all_events, key=lambda e: e.occurred_at, reverse=True)
                if e.event_type == "file_inspected"
            ]
        )
        touched = set(modified) | set(inspected)

        unfinished = self._unfinished(chain, chain_events)
        active = self._active_task(last, chain_events[last.id])

        # `task` is a BIAS, not a filter: the chain-relevant history is always returned and
        # items that share a word with the task are additionally marked relevant.
        decisions = await self._q.decisions(scope, limit=50)
        bugs = await self._q.bug_history(scope, limit=30)
        errors = await self._q.related_errors(scope, limit=30)
        attempts = await self._q.attempts(scope, limit=30)
        words = _task_words(task)
        decision_briefs = self._rank_decisions(
            decisions.decisions, chain_ids, touched, last, chain, words
        )
        chain_keys = {s.session_key for s in chain}

        bug_briefs = sorted(
            (
                BugBrief(
                    title=b.title,
                    status=b.status,
                    status_basis=b.status_basis,
                    last_activity=max(
                        (i.occurred_at for k in (b.discovered, b.errors, b.fixes) for i in k),
                        default=now,
                    ),
                    fix_attempts=len(b.fixes),
                    relevance=self._reasons(
                        in_chain=any(
                            i.session_id in chain_keys
                            for k in (b.discovered, b.errors, b.fixes)
                            for i in k
                        ),
                        files=touched,
                        named=[f for i in b.discovered for f in i.details.get("files", [])],
                        text=b.title,
                        words=words,
                    ),
                    sources=[i.source for k in (b.discovered, b.errors, b.fixes) for i in k][:5],
                )  # fmt: skip
                for b in bugs.bugs
            ),
            key=lambda b: (
                b.status not in _UNRESOLVED,
                -len(b.relevance),
                -b.last_activity.timestamp(),
            ),
        )
        error_briefs = sorted(
            (
                ErrorBrief(
                    title=e.title,
                    occurrences=e.occurrences,
                    last_seen=e.last_seen,
                    bug=e.bug,
                    files=e.files[:5],
                    fixed_by=[f.title for f in e.fixes if f.details.get("outcome") == "succeeded"][:3],
                    attempted_fixes=[f.title for f in e.fixes if f.details.get("outcome") != "succeeded"][:3],
                    relevance=self._reasons(
                        in_chain=any(i.session_id in chain_keys for i in e.events),
                        files=touched,
                        named=e.files,
                        text=e.title,
                        words=words,
                    ),
                )  # fmt: skip
                for e in errors.errors
            ),
            key=lambda e: (bool(e.fixed_by), -len(e.relevance), -e.last_seen.timestamp()),
        )
        attempt_briefs = [
            AttemptBrief(
                approach=a.approach,
                outcome=a.outcome,
                outcome_basis=a.outcome_basis,
                last_at=a.last_at,
                errors=a.errors[:3],
                relevance=self._reasons(
                    in_chain=any(i.session_id in chain_keys for i in a.events),
                    files=set(),
                    named=[],
                    text=a.approach,
                    words=words,
                ),
            )  # fmt: skip
            for a in sorted(attempts.attempts, key=lambda a: a.last_at, reverse=True)
        ]
        tests = [
            TestBrief(command=e.payload["command"], result=e.payload["result"], occurred_at=e.occurred_at, session_id=k, source=SourceRef(type="agent_event", id=e.id))
            for e, k in test_rows
        ]  # fmt: skip
        memories = await self._memories(scope, task or (active.text if active else None))

        if not include_unrelated:
            # M16: "avoid unrelated graph neighbours". Only items tied to the recent session
            # chain (or to files that work touched) are returned; `include_unrelated` opts
            # in to the whole project's history.
            decision_briefs = [d for d in decision_briefs if d.relevance]
            bug_briefs = [b for b in bug_briefs if b.relevance]
            error_briefs = [e for e in error_briefs if e.relevance]
            attempt_briefs = [a for a in attempt_briefs if a.relevance]

        sections = {
            "unfinished_work": cut("unfinished_work", unfinished),
            "recent_decisions": cut("recent_decisions", decision_briefs),
            "files_inspected": cut("files_inspected", inspected),
            "files_modified": cut("files_modified", modified),
            "recent_commits": cut("recent_commits", [_commit_ref(c) for c in recent_commits]),
            "known_bugs": cut("known_bugs", bug_briefs),
            "previous_errors": cut("previous_errors", error_briefs),
            "attempts": cut("attempts", attempt_briefs),
            "tests_run": cut("tests_run", tests),
            "important_memories": cut("important_memories", memories),
        }
        sources: list[SourceRef] = []
        seen: set[str] = set()
        for group in (
            [u.source for u in sections["unfinished_work"]], [d.source for d in sections["recent_decisions"]],
            [s for b in sections["known_bugs"] for s in b.sources], [t.source for t in sections["tests_run"]],
            [c.source for c in sections["recent_commits"]], [s for m in sections["important_memories"] for s in m.sources],
        ):  # fmt: skip
            for ref in group:
                if ref and ref.id not in seen:
                    seen.add(ref.id)
                    sources.append(ref)
        sections["relevant_sources"] = cut("relevant_sources", sources)

        return ResumeContext(
            generated_at=now,
            project={"id": scope.project_id, "name": project_name},
            has_history=True,
            last_session=self._brief(last, chain_events[last.id]),
            previous_sessions=[self._brief(s, chain_events[s.id]) for s in chain[1:]],
            active_task=active,
            unfinished_work=sections["unfinished_work"],
            recent_decisions=sections["recent_decisions"],
            files_inspected=sections["files_inspected"],
            files_modified=sections["files_modified"],
            recent_commits=sections["recent_commits"],
            known_bugs=sections["known_bugs"],
            previous_errors=sections["previous_errors"],
            attempts=sections["attempts"],
            tests_run=sections["tests_run"],
            important_memories=sections["important_memories"],
            relevant_sources=sections["relevant_sources"],
            ranking=RANKING,
            limits=limits,
        )

    # -- pieces ----------------------------------------------------------------------

    def _empty(
        self, scope: ProjectScope, name: str, limits: dict[str, SectionLimit]
    ) -> ResumeContext:
        return ResumeContext(
            generated_at=self._now(), project={"id": scope.project_id, "name": name}, has_history=False,
            last_session=None, previous_sessions=[], active_task=None, unfinished_work=[], recent_decisions=[],
            files_inspected=[], files_modified=[], recent_commits=[], known_bugs=[], previous_errors=[],
            attempts=[], tests_run=[], important_memories=[], relevant_sources=[], ranking=RANKING, limits=limits,
        )  # fmt: skip

    @staticmethod
    def _unique(items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))

    @staticmethod
    def _brief(s: AgentSession, events: list[AgentEvent]) -> SessionBrief:
        started = next((e for e in events if e.event_type == "session_started"), None)
        return SessionBrief(
            session_id=s.session_key, status=s.status, goal=_trim(started.payload.get("goal") if started else None),  # type: ignore[arg-type]
            outcome=s.outcome, summary=_trim(s.summary), started_at=s.started_at, ended_at=s.ended_at,  # type: ignore[arg-type]
            last_event_at=s.last_event_at,
            continues_session=started.payload.get("continues_session") if started else None,
        )  # fmt: skip

    @staticmethod
    def _active_task(last: AgentSession, events: list[AgentEvent]) -> ActiveTask | None:
        for e in sorted(events, key=lambda e: e.occurred_at, reverse=True):
            if e.event_type != "user_request":
                continue
            closed = any(
                x.event_type in ("task_completed", "task_blocked") and x.occurred_at > e.occurred_at
                for x in events
            )
            if not closed and not (last.status == "ended" and last.outcome == "completed"):
                return ActiveTask(
                    text=_trim(e.payload["text"]) or "", feature=e.payload.get("feature"),
                    session_id=last.session_key, occurred_at=e.occurred_at,
                    basis="latest_unclosed_user_request",
                    source=SourceRef(type="agent_event", id=e.id),
                )  # fmt: skip
        return None

    @staticmethod
    def _unfinished(
        chain: list[AgentSession], events: dict[str, list[AgentEvent]]
    ) -> list[UnfinishedItem]:
        out: list[UnfinishedItem] = []
        for s in chain:
            evs = sorted(events[s.id], key=lambda e: e.occurred_at)
            for e in evs:
                if e.event_type == "session_ended":
                    for text in e.payload.get("unfinished", []):
                        out.append(
                            UnfinishedItem(
                                text=_trim(text) or "",
                                basis="explicit_unfinished",
                                session_id=s.session_key,
                                occurred_at=e.occurred_at,
                                source=SourceRef(type="agent_event", id=e.id),
                            )
                        )
            for e in evs:
                if e.event_type == "task_blocked":
                    feature = e.payload.get("feature")
                    resolved = any(
                        x.event_type == "task_completed" and x.occurred_at > e.occurred_at and (not feature or x.payload.get("feature") == feature)
                        for c in chain for x in events[c.id]
                    )  # fmt: skip
                    if not resolved:
                        out.append(
                            UnfinishedItem(
                                text=_trim(e.payload["reason"]) or "",
                                basis="task_blocked",
                                session_id=s.session_key,
                                occurred_at=e.occurred_at,
                                source=SourceRef(type="agent_event", id=e.id),
                            )
                        )
            if not (s.status == "ended" and s.outcome == "completed"):
                for e in evs:
                    if e.event_type == "user_request" and not any(
                        x.event_type in ("task_completed", "task_blocked")
                        and x.occurred_at > e.occurred_at
                        for x in evs
                    ):
                        out.append(
                            UnfinishedItem(
                                text=_trim(e.payload["text"]) or "",
                                basis="unclosed_user_request",
                                session_id=s.session_key,
                                occurred_at=e.occurred_at,
                                source=SourceRef(type="agent_event", id=e.id),
                            )
                        )
        order = {"explicit_unfinished": 0, "task_blocked": 1, "unclosed_user_request": 2}
        out.sort(key=lambda u: (order[u.basis], -u.occurred_at.timestamp()))
        return out

    @staticmethod
    def _reasons(
        *,
        in_chain: bool,
        files: set[str],
        named: list[str],
        text: str = "",
        words: frozenset[str] = frozenset(),
    ) -> list[str]:
        reasons = []
        if words and words & _task_words(text):
            reasons.append("matches_task_words")
        if in_chain:
            reasons.append("in_recent_session_chain")
        if files and files & set(named):
            reasons.append("touches_a_file_this_work_touched")
        return reasons

    @staticmethod
    def _rank_decisions(
        decisions: list,
        chain_ids: set[str],
        touched: set[str],
        last: AgentSession,
        chain: list[AgentSession],
        words: frozenset[str] = frozenset(),
    ) -> list[DecisionBrief]:
        keys = {s.session_key for s in chain}
        briefs = []
        for d in decisions:
            reasons = []
            if d.session_id in keys:
                reasons.append("in_recent_session_chain")
            if touched & set(d.files):
                reasons.append("touches_a_file_this_work_touched")
            if words and words & _task_words(d.title + " " + (d.rationale.text if d.rationale else "")):
                reasons.append("matches_task_words")
            briefs.append(
                DecisionBrief(
                    title=d.title,
                    status=d.status,
                    rationale=_trim(d.rationale.text) if d.rationale else None,
                    files=d.files[:5],
                    session_id=d.session_id,
                    occurred_at=d.occurred_at,
                    superseded_by=d.superseded_by.title if d.superseded_by else None,
                    source=d.source,
                    relevance=reasons,
                )  # fmt: skip
            )
        # active before superseded; then by relevance reasons (heuristic); then newest.
        briefs.sort(
            key=lambda b: (b.status != "active", -len(b.relevance), -b.occurred_at.timestamp())
        )
        return briefs

    async def _memories(self, scope: ProjectScope, query: str | None) -> list[MemoryBrief]:
        if not query or self._retrieval is None:
            return []
        from app.projects.shaping import evidence_labels, source_refs

        try:
            results = await self._retrieval.search(user_id=scope.space_id, query=query[:500], limit=5)
        except AppError:
            return []  # supporting memories are optional; the Postgres-backed sections stand alone
        return [
            MemoryBrief(
                memory_id=r.memory.memory.id,
                content=_trim(r.memory.memory.content) or "",
                evidence=evidence_labels(r.memory.sources),
                score=round(r.explanation.final_score, 4),
                sources=source_refs(r.memory.sources),
            )  # fmt: skip
            for r in results
        ]

