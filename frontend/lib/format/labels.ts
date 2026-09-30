/**
 * Presentation vocabulary (docs/PHASE10_PLAN.md §4, ADR 0028). Pure value →
 * label/icon/explanation mappings. NO business logic lives here: statuses,
 * temporal states and evidence states are computed by the backend and only
 * translated into words. Every status has an icon AND text (never colour only).
 */
import type {
  DocumentStatus,
  EvidenceStatus,
  MemoryStatus,
  MemoryType,
  TemporalState,
} from "@/lib/api/types";

export interface Label {
  text: string;
  icon: string;
  tone: "current" | "warn" | "muted" | "danger" | "info" | "neutral";
  explain: string;
}

export const MEMORY_STATUS: Record<MemoryStatus, Label> = {
  active: {
    text: "Active",
    icon: "●",
    tone: "current",
    explain: "Believed by the AI. Whether it is true NOW depends on its validity window (see 'True from…').",
  },
  conflicting: {
    text: "Disputed",
    icon: "⚠",
    tone: "warn",
    explain: "Believed, but contradicted by another memory. Not resolved yet.",
  },
  historical: {
    text: "Historical",
    icon: "◌",
    tone: "muted",
    explain: "Was true; its validity window has closed. Kept for history.",
  },
  superseded: {
    text: "Superseded",
    icon: "↦",
    tone: "muted",
    explain: "Replaced by a newer memory. Kept, and used to answer questions about the past.",
  },
  invalid: {
    text: "Retracted",
    icon: "⊘",
    tone: "danger",
    explain: "Judged not true (backend status: invalid). Kept for the record; not used in answers.",
  },
  archived: { text: "Archived", icon: "▣", tone: "neutral", explain: "Filed away and excluded from retrieval." },
  merged: {
    text: "Duplicate (merged)",
    icon: "⧉",
    tone: "neutral",
    explain: "A confirmed duplicate of another memory. Kept, but the canonical memory is used instead.",
  },
};

export const TEMPORAL_STATE: Record<TemporalState, Label> = {
  current: { text: "True now", icon: "▶", tone: "current", explain: "Its validity window contains the chosen instant." },
  historical: {
    text: "No longer true",
    icon: "⏹",
    tone: "muted",
    explain: "Its validity window closed at or before the chosen instant.",
  },
  not_yet_valid: {
    text: "Not yet true",
    icon: "⏭",
    tone: "info",
    explain: "Its validity window starts after the chosen instant.",
  },
};

export const MEMORY_TYPE: Record<MemoryType, { text: string; explain: string }> = {
  semantic: { text: "Semantic", explain: "Something known." },
  episodic: { text: "Episodic", explain: "Something that happened." },
  procedural: { text: "Procedural", explain: "How something is done." },
  working: { text: "Working", explain: "Temporary task context." },
};

export const EVIDENCE_STATUS: Record<EvidenceStatus, { text: string; tone: Label["tone"] }> = {
  available: { text: "Evidence available", tone: "current" },
  no_asserting_memory: { text: "No asserting memory recorded for this relationship", tone: "warn" },
  memory_not_found: { text: "The asserting memory could not be found", tone: "warn" },
  memory_excluded: { text: "The asserting memory is excluded (retracted, archived or merged)", tone: "muted" },
  no_sources: { text: "The asserting memory has no recorded source", tone: "warn" },
  sources_unresolved: { text: "Sources were recorded but their content is no longer available", tone: "warn" },
};

export const DOCUMENT_STATUS: Record<DocumentStatus, { text: string; terminal: boolean; tone: Label["tone"] }> = {
  uploaded: { text: "Uploaded", terminal: false, tone: "info" },
  validating: { text: "Validating", terminal: false, tone: "info" },
  parsing: { text: "Reading text", terminal: false, tone: "info" },
  chunking: { text: "Splitting into chunks", terminal: false, tone: "info" },
  chunked: { text: "Chunked — waiting for extraction", terminal: false, tone: "info" },
  extracting: { text: "Extracting memories", terminal: false, tone: "info" },
  complete: { text: "Complete", terminal: true, tone: "current" },
  partially_complete: { text: "Partially complete", terminal: true, tone: "warn" },
  failed: { text: "Failed — not learned from", terminal: true, tone: "danger" },
};

export const DOCUMENT_PIPELINE: DocumentStatus[] = [
  "uploaded",
  "validating",
  "parsing",
  "chunking",
  "extracting",
  "complete",
];

export const TERMINATION_REASON: Record<string, string | null> = {
  final_answer: null,
  citation_stripped: "Some citations couldn't be verified against retrieved memories and were removed.",
  budget_exhausted: "The assistant reached its tool-call limit; the answer may be incomplete.",
  loop_detected: "The assistant stopped repeating the same lookup; the answer may be incomplete.",
  turn_timeout: "The assistant ran out of time; the answer may be incomplete.",
  malformed_output: "The model's output couldn't be understood; the answer may be incomplete.",
};

export const ANSWER_CONFIDENCE: Record<string, string> = {
  high: "High confidence",
  medium: "Medium confidence",
  low: "Low confidence",
  not_found: "No memory found",
};

export const CONFIDENCE_EXPLAIN =
  "Extraction confidence: the model's own score when it recorded this memory. It is metadata, not a probability that the memory is true.";

export const RELEVANCE_EXPLAIN =
  "Relevance ranks results within this search only. It is a retrieval signal, not a measure of truth.";

export const RETRIEVAL_CHANNEL: Record<string, string> = {
  vector: "meaning (semantic similarity)",
  graph: "connected entities (graph)",
  keyword: "exact words (keyword)",
};

// -- Phase 15: graph intelligence labels ---------------------------------------------------

/** What a predicate STATED. Never inferred from timing (docs/PHASE15_GRAPH_INTELLIGENCE.md). */
export const RELATION_KIND: Record<string, { text: string; explain: string }> = {
  causal: { text: "Stated as a cause", explain: "The memory itself states a cause or consequence (for example “led to” or “fixed by”)." },
  possible_cause: { text: "Possible cause", explain: "Stated as uncertain. This is not established as fact." },
  association: { text: "Association", explain: "The memory states a connection. It does not say one thing caused the other." },
};

export const EVIDENCE_BASIS: Record<string, { text: string; explain: string }> = {
  directly_stated: { text: "Stated directly", explain: "At least one stored memory asserts this relationship." },
  supported_by_multiple_memories: { text: "Several memories agree", explain: "More than one stored memory asserts this relationship." },
  inferred_from_graph: {
    text: "Inferred from the graph",
    explain: "Each step was stated, but the connection through the middle entities was not stated by anyone.",
  },
  temporally_derived: { text: "Not true now", explain: "Shown only because of the chosen time scope: it is not true at the current time." },
};

export const EVOLUTION_KIND: Record<string, string> = {
  new_fact: "First recorded",
  supersession: "Replaced an earlier memory",
  conflict: "Disagrees with another memory",
  conflict_resolved: "Conflict resolved by a reviewer",
  invalidation: "Retracted",
  merge: "Merged as a duplicate",
  reconfirmation: "Stated again",
};
