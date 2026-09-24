"""The Phase 5 agent contract (docs/PHASE5_PLAN.md).

Mirrors `app/models/extraction.py`'s discipline exactly, because the threat
model is the same: this is untrusted model output (an LLM's structured
response), validated strictly (`extra="forbid"`, bounded lengths/lists)
before anything downstream sees it.

**No schema in this module has a `user_id` field, on either the request or
the tool-argument side.** That is the single load-bearing security property
of the whole agent tool layer -- `user_id` is always a Python parameter
supplied by `AgentService` from the authenticated request context, never a
value an LLM can populate. See `app/agent/tools.py`'s module docstring for
how dispatch enforces this.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import MemoryType
from app.models.extraction import (
    CandidateEntity,
    CandidateRelationship,
    CandidateTemporal,
    Confidence,
    Persistence,
)

TemporalScope = Literal["current", "historical", "as_of"]
AnswerConfidence = Literal["high", "medium", "low", "not_found"]

#: Every tool name the agent may select -- the single source of truth for
#: both `ToolCallStep.tool_name`'s allowed values and
#: `app.agent.tools.TOOL_DISPATCH`'s keys (kept in sync by
#: `test_agent_models.py::test_every_tool_name_has_a_dispatch_entry`).
ToolName = Literal[
    "search_memory",
    "get_entity",
    "traverse_graph",
    "get_entity_state",
    "get_memory_history",
    "get_source",
    "propose_memory_update",
]


# ---------------------------------------------------------------------------
# Tool argument schemas -- one per tool, all extra="forbid", all bounded.
# ---------------------------------------------------------------------------


class SearchMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=10, ge=1, le=25)
    #: Explicit override, layered on top of (never replacing)
    #: `detect_temporal_intent`'s existing text-based detection when omitted
    #: -- PHASE5_PLAN.md section 8.
    temporal_scope: TemporalScope | None = None
    as_of: datetime | None = None

    @model_validator(mode="after")
    def _as_of_requires_date(self) -> SearchMemoryArgs:
        if self.temporal_scope == "as_of" and self.as_of is None:
            raise ValueError("as_of is required when temporal_scope='as_of'")
        return self


class GetEntityArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name_or_id: str = Field(min_length=1, max_length=200)


class TraverseGraphArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=200)
    #: Clamped again, server-side, to settings.retrieval_graph_hops's own
    #: hard bound at dispatch time -- this field's own le=3 is a first,
    #: cheap validation layer, not the only one.
    max_hops: int = Field(default=2, ge=1, le=3)
    #: Applied as a Python-side post-filter over returned paths (v1) --
    #: never a new Cypher parameter (PHASE5_PLAN.md section 3).
    relationship_filter: str | None = Field(default=None, max_length=100)


class GetEntityStateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=200)
    as_of: datetime | None = None


class GetMemoryHistoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(min_length=1, max_length=200)


class GetSourceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(min_length=1, max_length=200)


class ProposeMemoryUpdateArgs(BaseModel):
    """Deliberately shaped like `app.models.extraction.CandidateMemory` minus
    `source_message_id`/`source_span` (server-supplied at dispatch time,
    never from the model) -- so the agent's proposal becomes a genuine
    `CandidateMemory` with no translation layer that could drift from the
    extraction contract it must be indistinguishable from downstream.
    """

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=2000)
    memory_type: MemoryType
    persistence: Persistence
    confidence: Confidence
    entities: list[CandidateEntity] = Field(default_factory=list, max_length=20)
    relationships: list[CandidateRelationship] = Field(default_factory=list, max_length=20)
    temporal: CandidateTemporal = Field(default_factory=CandidateTemporal)
    reasoning: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _relationship_endpoints_are_declared(self) -> ProposeMemoryUpdateArgs:
        """Identical rule to `CandidateMemory`'s own validator -- duplicated
        (not imported) because Pydantic model validators are not inherited
        across unrelated classes, but the RULE itself must not drift from
        the one `CandidateMemory` enforces.
        """
        known = {e.name.strip().lower() for e in self.entities}
        for rel in self.relationships:
            for endpoint in (rel.source, rel.target):
                if endpoint.strip().lower() not in known:
                    raise ValueError(
                        f"relationship endpoint {endpoint!r} is not listed in "
                        f"this candidate's entities"
                    )
        return self


TOOL_ARGS_SCHEMAS: dict[ToolName, type[BaseModel]] = {
    "search_memory": SearchMemoryArgs,
    "get_entity": GetEntityArgs,
    "traverse_graph": TraverseGraphArgs,
    "get_entity_state": GetEntityStateArgs,
    "get_memory_history": GetMemoryHistoryArgs,
    "get_source": GetSourceArgs,
    "propose_memory_update": ProposeMemoryUpdateArgs,
}


# ---------------------------------------------------------------------------
# The bounded tool-selection discriminated union.
# ---------------------------------------------------------------------------


class ToolCallStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["tool_call"] = "tool_call"
    tool_name: ToolName
    #: A JSON-encoded object, e.g. '{"query": "PaddleOCR"}' -- a STRING, not a
    #: nested object. Two constraints forced this rather than
    #: `dict[str, Any]`: (a) a discriminated union of heterogeneous nested
    #: Args models per tool is not expressible as a single Pydantic schema
    #: `complete_structured` can target (see `app.agent.tools`'s docstring),
    #: and (b) discovered against the real Gemini Developer API (not just a
    #: theoretical concern): its structured-output mode rejects any schema
    #: needing `additionalProperties` -- which Pydantic emits for ANY dict
    #: field, typed or not -- with "additionalProperties is only supported in
    #: Gemini Enterprise Agent Platform mode, not in Gemini Developer API
    #: mode". A JSON string has no `additionalProperties` in its own schema
    #: (it is just `{"type": "string"}`), so it is universally representable.
    #: Parsed and validated against `TOOL_ARGS_SCHEMAS[tool_name]` in
    #: `AgentService._run_loop` immediately after this step itself validates;
    #: invalid JSON here is treated exactly like a malformed structured
    #: response (one repair attempt, then a forced answer) -- never a crash.
    tool_args_json: str = Field(max_length=4000)


class FinalAnswerStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["final_answer"] = "final_answer"
    answer: str = Field(min_length=1, max_length=4000)
    #: Structurally validated by the loop against every memory id any tool
    #: call actually returned this turn (PHASE5_PLAN.md section 9) -- never
    #: trusted at face value.
    cited_memory_ids: list[str] = Field(default_factory=list, max_length=50)
    confidence: AnswerConfidence = "medium"


class AgentStep(BaseModel):
    """The `complete_structured` target for one agent-loop iteration.

    A flat wrapper rather than a bare `ToolCallStep | FinalAnswerStep` union
    because some providers' structured-output modes require one top-level
    object, not a union, as the target schema; `step` carries the real
    decision either way.

    **No `Field(discriminator="action")`** -- that was the original design,
    and it round-trips fine through `FakeLLMClient`
    (`model_validate_json`/`model_dump_json` don't care how a union is
    declared), but discovered against the real Gemini Developer API: a
    `discriminator`-tagged union renders as JSON Schema's `discriminator` +
    `oneOf` keywords, and Gemini's own `Schema` type (an OpenAPI SUBSET, not
    full JSON Schema) rejects both with `pydantic.ValidationError: Extra
    inputs are not permitted` while building the request -- the call never
    even reaches the network. A plain (non-discriminated) union renders as
    `anyOf`, which Gemini's `Schema` type does accept. Validation correctness
    is unaffected: Pydantic's "smart mode" union resolution still picks the
    right member unambiguously, since `ToolCallStep`/`FinalAnswerStep` each
    still declare a distinct `action: Literal[...]` value -- discrimination
    happens the same way, just not via this specific JSON Schema keyword.
    """

    model_config = ConfigDict(extra="forbid")

    step: ToolCallStep | FinalAnswerStep
