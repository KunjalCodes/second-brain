/** Presentation of coding-agent event types (docs/OBSIDIAN_UI_PLAN.md §6). Words and glyphs only. */
import type { Tone } from "./glyphs";

export interface EventStyle {
  glyph: string;
  label: string;
  tone: Tone;
}

const S = (glyph: string, label: string, tone: Tone): EventStyle => ({ glyph, label, tone });

const STYLES: Record<string, EventStyle> = {
  session_started: S("⌁", "Session started", "muted"),
  session_ended: S("⌁", "Session ended", "muted"),
  user_request: S("❝", "Request", "accent2"),
  decision: S("⚖", "Decision", "accent"),
  file_inspected: S("▭", "File inspected", "muted"),
  file_modified: S("Δ", "File changed", "info"),
  error: S("✕", "Error", "danger"),
  bug_discovered: S("⚠", "Bug found", "danger"),
  fix: S("✓", "Fix", "current"),
  test_run: S("⚑", "Test", "current"),
  commit: S("⦿", "Commit", "info"),
  task_completed: S("✓", "Task completed", "current"),
  task_blocked: S("■", "Blocked", "warn"),
  unfinished_work: S("◐", "Unfinished", "warn"),
};

export function eventStyle(type: string): EventStyle {
  return STYLES[type] ?? S("•", type.replace(/_/g, " "), "muted");
}

/** Raw bookkeeping that adds noise to a "what did the agent do" story. */
export const NOISY_EVENT_TYPES = new Set(["file_inspected", "session_started", "session_ended"]);

export const EVIDENCE_TEXT: Record<string, string> = {
  git: "From Git",
  agent_stated: "Stated by the agent",
  parser: "From the code parser",
  derived: "Derived by the system",
};
