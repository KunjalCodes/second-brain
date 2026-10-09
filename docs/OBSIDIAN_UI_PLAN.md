# Obsidian-style UI redesign — plan

UI/UX only. No new backend endpoints, databases, graph libraries or providers. Labels:
**[I]** inspected, **[A]** assumption, **[L]** limitation.

## 1. Current UI (audited) [I]

* Next.js 16 / React 19 / Tailwind 4 / TanStack Query / Cytoscape 3.34 (lazy-loaded). ~5.7k lines
  of hand-written TSX (plus 7.8k generated API types).
* **All colour comes from CSS variables** (`--color-*` in `app/globals.css`), used as
  `var(--color-…)` in components. Re-theming is therefore a token change, not a rewrite.
* Layout: `AppShell` = flat 8-link sidebar + top bar (project switcher, time lens). Pages are
  stacked bordered cards ("admin dashboard").
* Routes: `/` Overview, `/memories[/id[/timeline]]`, `/entities[/id]`, `/graph`, `/search`,
  `/documents[/id]`, `/review[/memories|entities/id]`, `/ask`, `/conversations/id`, `/settings`,
  `/login`, `/register`.
* Graph: `GraphExplorer` (state/filters/paths, 374 lines) → `CytoscapeCanvas` (stylesheet, already
  shape-per-entity-type) → `GraphDetail` (node/edge panel) with server budgets, truncation banner,
  list-view alternative, path finder, evidence/explain panels.
* State: time lens in React context (in memory only); memory space in `lib/api/space.ts` sent as
  `X-Memory-Space` (never on `/auth` or `/projects`); `qk` query keys; `apiRequest` with cookie
  credentials (no token in web storage).
* Tests: 96 Vitest tests, 40 Playwright tests (incl. axe, CSP, isolation, projects).

## 2. Problems

1. Flat navigation of database tables; no sense of *workspace*, *project* or *activity*.
2. Cards everywhere; Overview is three counters and a list.
3. Graph looks like a diagram, not a knowledge map: one accent colour, no type icons/legend, the
   selected-node panel lacks related knowledge/history/sources, no search-to-focus.
4. **Project memory has no UI beyond the space switcher**: sessions, decisions, history, resume,
   commits exist in the API (`/projects/{id}/…`) but nothing in the frontend calls them. [I]
5. Memory/Ask/entity pages do not make the trace *Memory → Evidence → Source → History* a path.
6. Technical-looking palette (blue accent on cool grey).

## 3. Information architecture

Grouped sidebar (collapsible; icon-only when collapsed; persisted in `localStorage` as a pure
UI preference):

* **Workspace** — Overview · Memories · Entities · Graph · Projects · Documents
* **Explore** — Search
* **Intelligence** — Ask · Review
* **Account** — Settings

Cross-links implement the path *Memory → Entity → Relationship → Source → History → Project →
Agent session* (all links, no new data). Search/timeline stay inside their parent pages; a
separate Timeline/Sources route would only duplicate memory-history and provenance. [A]

## 4. Visual direction

Calm, warm, knowledge-first. Warm off-white / deep warm charcoal base; **muted teal** primary
accent; dusty lavender (secondary: entities), sage (current/success), muted rose (disputed/danger),
ochre (warning), soft blue-grey (info). Serif-leaning reading type for memory statements
(`Georgia`-family system stack — no web-font download), system sans for UI. Glass is used **only**
on the top bar, side panels and overlays (`backdrop-filter` with an opaque fallback); content
surfaces are solid. Tokens: bg, surface, surface-2, glass, border, fg, fg-muted, muted, accent,
accent-2, current, warn, danger, info, plus radius/shadow tokens. Contrast ≥ 4.5:1 for text in
both themes (checked with axe in Playwright). Reduced-motion respected.

## 5. Graph redesign

* **Node language** (never colour alone): shape + glyph prefix in the label + border style +
  fill tint + size. Groups: Memory, Entity (person/company/concept), Technology, Project, File /
  Directory, Document, Decision, Bug/Error, Fix/Test, Agent session, Commit, Source.
* Calm canvas: dotted-grid background, softer edges, label on hover/selection for dense graphs,
  focus+neighbour emphasis (others dim) on selection, smooth animated fit.
* **Search box on the canvas** (existing `entities/search`) → re-root/focus.
* **Contextual panel** (right, glass) replaces the generic detail box: type badge, name, aliases,
  *Related knowledge* (from existing `GET /entities/{id}/context`), *Recent history* (its
  timeline), *Sources* (links from context memories), actions (expand, focus, find path, collapse).
  Edge panel keeps explain/evidence.
* **Legend** panel with real glyphs. List view stays the accessible alternative (default on
  narrow screens).
* **Time**: a visible "Knowledge as of …" banner on the canvas (Now / As-of / historical-only),
  current vs historical edges differ by line style *and* a label chip. Server budgets and
  truncation banner are unchanged. [I] no timeline scrubber: a scrubber needs only the existing
  as-of control; the top-bar lens stays the single source.

## 6. Pages

* **Overview** → "What's happening": continue-where-you-left-off (latest conversation / project
  resume), recent memories, projects with last activity, documents processing, review items,
  recent agent sessions. No KPI-card grid.
* **Memories** list → statement-first rows (serif), meta line, progressive disclosure.
  **Memory detail** → "Why the AI remembers this" first: a trace strip *Memory → Evidence →
  Source → History*, then the existing panels (they already show provenance/explanation/evolution).
* **Entity** → hub: header with type, At-a-glance (existing), relationships, memories, "Open in
  graph".
* **Projects (new)** — `/projects`: project list (from `GET /projects`). `/projects/[id]`:
  overview from `GET /projects/{id}/resume` + `/decisions` + `/history` + `/sessions`:
  unfinished work (only when explicit), recent decisions, bugs, files, commits, sessions.
  `/projects/[id]/sessions/[sid]`: "what the agent did" timeline grouped by meaningful event
  types (request, files, decisions, errors, fixes, tests, commit), from `GET …/sessions/{sid}`.
  Not fabricated: empty sections say so.
* **Documents** → passages are already listed per document; add clearer "Document → Passage →
  Memory" wording and links where the API provides them.
* **Search** → grouped by what matched and why (existing explanation fields), result →
  memory/entity/graph links.
* **Ask** → answer first, then "Evidence" with cited memories (server-validated ids only),
  each linking to the memory and its source; temporal context line from the time lens.

## 7. Components

New shared primitives in `components/ui/`: `Surface` (solid/raised/glass), `Pill`, `Section`
(title + progressive disclosure), `Glyph` (type → glyph/label), `TraceStrip`, `Timeline`
(vertical, typed dots). Existing `Button`/`Chip`/`States` restyled through tokens. No new UI
library.

## 8. Accessibility / performance / security

* Keyboard: collapsible nav with `aria-expanded`, panels are landmarks (`aside`/`nav`), canvas
  keeps its list-view alternative; visible focus ring uses the accent; `prefers-reduced-motion`
  disables transitions and graph animation.
* Performance: no new runtime dependency; Cytoscape stays lazy; transitions CSS-only; project
  pages fetch bounded endpoints; graph budgets and `exclude_ids` logic untouched. Bundle size
  and graph render timing are measured before/after (see report).
* Security: no HTML injection (all backend text rendered as text), CSP untouched, no new
  storage except a UI preference (`nav-collapsed`), no client-controlled identity.

## 9. APIs reused / missing

Reused: everything already wrapped, plus **existing but unwrapped** project routes
(`/projects/{id}/resume`, `/decisions`, `/history`, `/sessions`, `/sessions/{sid}`, `/bugs`,
`/commits/{sha}`) via new typed wrappers.
Missing (documented, **not added**): a cross-memory "recent activity" feed, a graph
timeline-range endpoint, and a project-level aggregate "important entities" endpoint. The
Overview and project pages compose existing endpoints instead. [L]

## 10. Increments

1. Tokens + shell + primitives. 2. Graph. 3. Overview. 4. Projects & sessions. 5. Memory/Ask/
Entity/Search/Documents polish. 6. Tests, E2E, a11y, measurements, report, one commit.
