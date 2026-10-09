/**
 * The visual vocabulary for kinds of knowledge (docs/OBSIDIAN_UI_PLAN.md §5). Pure
 * presentation: a glyph, a human label and a group per entity type. The glyph is always
 * paired with the text label wherever it is shown, and the graph adds a distinct SHAPE
 * on top of it, so no meaning depends on colour or on the glyph alone.
 */
export type Tone = "accent" | "accent2" | "current" | "warn" | "danger" | "info" | "muted";

export interface Kind {
  glyph: string;
  label: string;
  group: "Knowledge" | "Code" | "Work" | "Source";
  tone: Tone;
}

const K = (glyph: string, label: string, group: Kind["group"], tone: Tone): Kind => ({ glyph, label, group, tone });

export const ENTITY_KIND: Record<string, Kind> = {
  person: K("☺", "Person", "Knowledge", "accent2"),
  company: K("▣", "Company", "Knowledge", "accent2"),
  concept: K("◇", "Concept", "Knowledge", "accent2"),
  technology: K("⚙", "Technology", "Knowledge", "info"),
  project: K("◆", "Project", "Knowledge", "accent"),
  document: K("▤", "Document", "Source", "muted"),
  event: K("✧", "Event", "Knowledge", "accent2"),
  task: K("☐", "Task", "Work", "info"),
  other: K("○", "Other", "Knowledge", "muted"),
  decision: K("⚖", "Decision", "Work", "accent"),
  feature: K("★", "Feature", "Work", "accent"),
  change: K("Δ", "Change", "Work", "info"),
  repository: K("⎇", "Repository", "Code", "info"),
  branch: K("⑂", "Branch", "Code", "info"),
  directory: K("▸", "Directory", "Code", "info"),
  file: K("▭", "File", "Code", "info"),
  code_symbol: K("ƒ", "Code symbol", "Code", "info"),
  dependency: K("⬡", "Dependency", "Code", "info"),
  bug: K("⚠", "Bug", "Work", "danger"),
  error: K("✕", "Error", "Work", "danger"),
  fix: K("✓", "Fix", "Work", "current"),
  test_run: K("⚑", "Test run", "Work", "current"),
  agent_session: K("⌁", "Agent session", "Work", "accent"),
  commit: K("⦿", "Commit", "Code", "info"),
};

export const MEMORY_KIND: Kind = K("❝", "Memory", "Knowledge", "muted");
export const SOURCE_KIND: Kind = K("⌗", "Source", "Source", "muted");

export function kindOf(kind: string | null | undefined, entityType?: string | null): Kind {
  if (kind === "memory") return MEMORY_KIND;
  if (kind === "source") return SOURCE_KIND;
  return ENTITY_KIND[entityType ?? ""] ?? ENTITY_KIND.other!;
}

export const TONE_VAR: Record<Tone, string> = {
  accent: "var(--color-accent)",
  accent2: "var(--color-accent-2)",
  current: "var(--color-current)",
  warn: "var(--color-warn)",
  danger: "var(--color-danger)",
  info: "var(--color-info)",
  muted: "var(--color-muted)",
};
