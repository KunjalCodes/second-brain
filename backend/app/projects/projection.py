"""Event -> memory candidate (Phase 16, M8): a PURE, deterministic mapping.

No I/O, no LLM. Given a validated event it returns the candidate fact the existing
governance pipeline will classify (duplicate / conflict / supersession) and persist,
or a reason it is not worth remembering.

Evidence rules this mapping enforces:

* A relationship is emitted only if the event STATES it. An error is linked to a bug
  only if the event names the bug; a fix is `fixed_by` only if its stated outcome was
  `succeeded` (`partially_fixed_by` / `attempted_fix` otherwise). File changes alone
  never produce an attempt, a failure or a cause.
* Everything an agent says is attributed to the agent session. Confidence is 0.9
  (explicit statement, still metadata, not truth).
* Structural facts (file inspected / modified) skip embeddings; semantic ones keep
  them (docs/PHASE16_ARCHITECTURE.md section 5).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import EntityType, MemoryType
from app.models.extraction import (
    CandidateEntity,
    CandidateMemory,
    CandidateRelationship,
    CandidateTemporal,
    Span,
)
from app.models.project_history import AgentEventType
from app.projects.events import MAX_PROPOSAL_CONFIDENCE, PROPOSABLE_TITLED
from app.projects.naming import (
    commit_name,
    file_name,
    session_name,
    titled_name,
)

AGENT_CONFIDENCE = 0.9
MAX_STATEMENT = 1990


@dataclass(frozen=True)
class ProjectedFact:
    candidate: CandidateMemory | None
    #: Embed this memory? False for structural facts.
    embed: bool = True
    #: Title of an earlier DECISION this one explicitly replaces.
    supersedes_title: str | None = None
    #: Why nothing is remembered (candidate is None).
    skip_reason: str | None = None


def _cut(text: str, limit: int = MAX_STATEMENT) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _first_line(text: str, limit: int) -> str:
    line = text.splitlines()[0] if text.splitlines() else text
    return line[:limit].rstrip()


class _Builder:
    def __init__(self, session_key: str) -> None:
        self.session = session_name(session_key)
        self.entities: dict[str, CandidateEntity] = {}
        self.relationships: list[CandidateRelationship] = []
        self.rel_confidence = AGENT_CONFIDENCE
        self.add(self.session, EntityType.AGENT_SESSION)

    def add(self, name: str, kind: EntityType) -> str:
        self.entities.setdefault(name, CandidateEntity(name=name, entity_type=kind))
        return name

    def rel(self, source: str, predicate: str, target: str) -> None:
        item = CandidateRelationship(
            source=source, predicate=predicate, target=target, confidence=self.rel_confidence
        )
        if item not in self.relationships:
            self.relationships.append(item)

    def files(self, paths: list[str]) -> list[str]:
        return [self.add(file_name(p), EntityType.FILE) for p in paths]

    def feature(self, title: str | None) -> str | None:
        return self.add(titled_name("feature", title), EntityType.FEATURE) if title else None

    def build(
        self,
        *,
        statement: str,
        memory_type: MemoryType,
        reasoning: str,
        embed: bool,
        event_id: str,
        supersedes_title: str | None = None,
        is_change: bool = False,
        confidence: float = AGENT_CONFIDENCE,
    ) -> ProjectedFact:
        statement = _cut(statement)
        candidate = CandidateMemory(
            statement=statement,
            memory_type=memory_type,
            persistence="persistent",
            confidence=confidence,
            entities=list(self.entities.values()),
            relationships=self.relationships,
            temporal=CandidateTemporal(is_change=is_change),
            source_message_id=event_id,
            source_span=Span(start=0, end=len(statement)),
            reasoning=reasoning,
        )
        return ProjectedFact(candidate, embed=embed, supersedes_title=supersedes_title)


def _bullets(label: str, items: list[str]) -> str:
    return f" {label}: " + "; ".join(items) + "." if items else ""


def build_fact(
    *,
    event_type: AgentEventType,
    payload: dict,
    session_key: str,
    event_id: str,
) -> ProjectedFact:
    """The candidate fact for one event, or a skip reason."""
    b = _Builder(session_key)
    key = session_key
    why = f"agent event {event_type.value} in session {key}"
    E = AgentEventType  # noqa: N806 - a short alias keeps the table readable

    if event_type is E.SESSION_STARTED:
        goal, previous = payload.get("goal"), payload.get("continues_session")
        if previous:
            b.rel(b.session, "continues", b.add(session_name(previous), EntityType.AGENT_SESSION))
        if not goal and not previous:
            return ProjectedFact(None, skip_reason="session start carries nothing to remember")
        text = f"Agent session {key} started" + (f": {goal}" if goal else ".")
        if previous:
            text += f" It continues session {previous}."
        return b.build(
            statement=text, memory_type=MemoryType.EPISODIC, reasoning=why, embed=False,
            event_id=event_id,
        )  # fmt: skip

    if event_type is E.SESSION_ENDED:
        outcome = payload["outcome"]
        text = f"Agent session {key} ended ({outcome})."
        if payload.get("summary"):
            text += f" {payload['summary']}"
        text += _bullets("Unfinished", payload.get("unfinished", []))
        return b.build(
            statement=text, memory_type=MemoryType.EPISODIC, reasoning=why, embed=True,
            event_id=event_id,
        )  # fmt: skip

    if event_type is E.USER_REQUEST:
        feature = b.feature(payload.get("feature"))
        if feature:
            b.rel(b.session, "worked_on", feature)
        return b.build(
            statement=f"User request in session {key}: {payload['text']}",
            memory_type=MemoryType.EPISODIC, reasoning=why, embed=True, event_id=event_id,
        )  # fmt: skip

    if event_type is E.DECISION:
        decision = b.add(titled_name("decision", payload["title"]), EntityType.DECISION)
        b.rel(b.session, "made", decision)
        for f in b.files(payload.get("files", [])):
            b.rel(decision, "affected", f)
        feature = b.feature(payload.get("feature"))
        if feature:
            b.rel(decision, "affected", feature)
        old = payload.get("supersedes_decision")
        if old:
            old_name = b.add(titled_name("decision", old), EntityType.DECISION)
            b.rel(old_name, "superseded_by", decision)
        text = f"Decision in session {key}: {payload['title']}."
        if payload.get("rationale"):
            text += f" Rationale (stated by the agent): {payload['rationale']}"
        text += _bullets("Alternatives considered", payload.get("alternatives", []))
        if old:
            text += f" This replaces the earlier decision: {old}."
        return b.build(
            statement=text, memory_type=MemoryType.SEMANTIC, reasoning=why, embed=True,
            event_id=event_id, supersedes_title=old, is_change=bool(old),
        )  # fmt: skip

    if event_type is E.FILE_INSPECTED:
        f = b.files([payload["path"]])[0]
        b.rel(b.session, "inspected", f)
        text = f"Session {key} inspected {payload['path']}."
        if payload.get("note"):
            text += f" Note: {payload['note']}"
        return b.build(
            statement=text, memory_type=MemoryType.EPISODIC, reasoning=why, embed=False,
            event_id=event_id,
        )  # fmt: skip

    if event_type is E.FILE_MODIFIED:
        change = payload.get("change", "modified")
        f = b.files([payload["path"]])[0]
        predicate = {"created": "created", "deleted": "deleted"}.get(change, "modified")
        b.rel(b.session, predicate, f)
        text = f"Session {key} {change} {payload['path']}"
        if change == "renamed":
            old = b.files([payload["old_path"]])[0]
            b.rel(f, "renamed_from", old)
            text += f" (from {payload['old_path']})"
        text += "."
        if payload.get("summary"):
            text += f" {payload['summary']}"
        return b.build(
            statement=text, memory_type=MemoryType.EPISODIC, reasoning=why, embed=False,
            event_id=event_id,
        )  # fmt: skip

    if event_type is E.BUG_DISCOVERED:
        bug = b.add(titled_name("bug", payload["title"]), EntityType.BUG)
        b.rel(b.session, "discovered", bug)
        for f in b.files(payload.get("files", [])):
            b.rel(bug, "affected", f)
        feature = b.feature(payload.get("feature"))
        if feature:
            b.rel(bug, "affected", feature)
        text = f"Bug discovered in session {key}: {payload['title']}."
        if payload.get("description"):
            text += f" {payload['description']}"
        return b.build(
            statement=text, memory_type=MemoryType.EPISODIC, reasoning=why, embed=True,
            event_id=event_id,
        )  # fmt: skip

    if event_type is E.ERROR:
        label = payload.get("title") or _first_line(payload["message"], 160)
        error = b.add(titled_name("error", label), EntityType.ERROR)
        b.rel(b.session, "encountered", error)
        for f in b.files(payload.get("files", [])):
            b.rel(error, "affected", f)
        if payload.get("bug"):
            bug = b.add(titled_name("bug", payload["bug"]), EntityType.BUG)
            b.rel(bug, "manifested_as", error)
        text = f"Error encountered in session {key}: {payload['message']}"
        if payload.get("command"):
            text += f" (command: {payload['command']})"
        if payload.get("approach"):
            text += f" While trying approach: {payload['approach']}."
        return b.build(
            statement=text, memory_type=MemoryType.EPISODIC, reasoning=why, embed=True,
            event_id=event_id,
        )  # fmt: skip

    if event_type is E.FIX:
        fix = b.add(titled_name("fix", payload["title"]), EntityType.FIX)
        b.rel(b.session, "made", fix)
        predicate = {
            "succeeded": "fixed_by",
            "partial": "partially_fixed_by",
            "failed": "attempted_fix",
        }[payload["outcome"]]
        if payload.get("fixes_error"):
            b.rel(
                b.add(titled_name("error", payload["fixes_error"]), EntityType.ERROR),
                predicate,
                fix,
            )
        if payload.get("fixes_bug"):
            b.rel(b.add(titled_name("bug", payload["fixes_bug"]), EntityType.BUG), predicate, fix)
        for f in b.files(payload.get("files", [])):
            b.rel(fix, "modified", f)
        text = (
            f"Fix in session {key}: {payload['title']}. "
            f"Outcome (stated by the agent): {payload['outcome']}."
        )
        if payload.get("description"):
            text += f" {payload['description']}"
        if payload.get("approach"):
            text += f" Approach: {payload['approach']}."
        return b.build(
            statement=text, memory_type=MemoryType.EPISODIC, reasoning=why, embed=True,
            event_id=event_id,
        )  # fmt: skip

    if event_type is E.TEST_RUN:
        run = b.add(f"test_run:{event_id[:12]}", EntityType.TEST_RUN)
        b.rel(b.session, "ran", run)
        link = "validated" if payload["result"] == "passed" else "failed_on"
        for title in payload.get("validates", []):
            b.rel(run, link, b.add(titled_name("fix", title), EntityType.FIX))
        for f in b.files(payload.get("files", [])):
            b.rel(run, "covered", f)
        text = f"Test run in session {key}: `{payload['command']}` {payload['result']}."
        if payload.get("passed") is not None or payload.get("failed") is not None:
            text += f" Passed: {payload.get('passed', '?')}, failed: {payload.get('failed', '?')}."
        if payload.get("summary"):
            text += f" {payload['summary']}"
        return b.build(
            statement=text, memory_type=MemoryType.EPISODIC, reasoning=why, embed=False,
            event_id=event_id,
        )  # fmt: skip

    if event_type is E.COMMIT:
        commit = b.add(commit_name(payload["sha"]), EntityType.COMMIT)
        b.rel(b.session, "committed", commit)
        for f in b.files(payload.get("files", [])):
            b.rel(commit, "changed", f)
        return b.build(
            statement=f"Session {key} committed {payload['sha'][:12]}: {payload['message']}",
            memory_type=MemoryType.EPISODIC, reasoning=why, embed=True, event_id=event_id,
        )  # fmt: skip

    if event_type is E.TASK_COMPLETED:
        feature = b.feature(payload.get("feature"))
        if feature:
            b.rel(b.session, "completed", feature)
        return b.build(
            statement=f"Task completed in session {key}: {payload['summary']}",
            memory_type=MemoryType.EPISODIC, reasoning=why, embed=True, event_id=event_id,
        )  # fmt: skip

    if event_type is E.TASK_BLOCKED:
        feature = b.feature(payload.get("feature"))
        if feature:
            b.rel(b.session, "blocked_on", feature)
        text = f"Task blocked in session {key}: {payload['reason']}"
        if payload.get("needs"):
            text += f" Needs: {payload['needs']}"
        if payload.get("approach"):
            text += f" Approach in progress: {payload['approach']}."
        return b.build(
            statement=text, memory_type=MemoryType.EPISODIC, reasoning=why, embed=True,
            event_id=event_id,
        )  # fmt: skip

    if event_type is E.MEMORY_PROPOSAL:
        # Full governance applies (duplicate / conflict / supersession classification).
        # Names are the agent's, so structural kinds are not proposable (validated at the
        # boundary) and titled kinds get the same prefix typed events use.
        def name_of(entity: dict) -> str:
            kind = EntityType(entity["entity_type"])
            return (
                titled_name(kind.value, entity["name"])
                if kind in PROPOSABLE_TITLED
                else entity["name"]
            )

        confidence = min(payload.get("confidence", 0.7), MAX_PROPOSAL_CONFIDENCE)
        b.rel_confidence = confidence
        by_raw = {e["name"].lower(): name_of(e) for e in payload.get("entities", [])}
        for e in payload.get("entities", []):
            b.add(by_raw[e["name"].lower()], EntityType(e["entity_type"]))
        for r in payload.get("relationships", []):
            b.rel(by_raw[r["source"].lower()], r["predicate"], by_raw[r["target"].lower()])
        b.entities.pop(b.session, None)  # attribution lives in the source, not the graph
        return b.build(
            statement=payload["statement"],
            memory_type=MemoryType(payload.get("memory_type", "semantic")),
            reasoning=payload.get("reasoning") or why,
            embed=True,
            event_id=event_id,
            confidence=confidence,
        )

    raise AssertionError(f"unhandled event type {event_type}")  # pragma: no cover
