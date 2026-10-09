# Obsidian-style UI redesign — report

Scope: frontend only. No backend endpoint, database, graph library or provider was added.
Labels: **[V]** verified by a test/run, **[M]** measured, **[I]** inspected (looked at, not
asserted), **[A]** assumption, **[L]** limitation. Plan: `docs/OBSIDIAN_UI_PLAN.md`.

## 1. What changed and why

| Area | Change | Why |
|---|---|---|
| Design system | New tokens in `app/globals.css` (warm off-white / deep warm charcoal; teal accent, lavender entities, sage = current, rose = danger, ochre = warning), `.glass` / `.panel` / `.reading` / `.dotgrid` utilities, reduced-motion support | The product looked like a cool-grey admin tool. All components already used `var(--color-*)`, so re-theming did not need a rewrite. |
| Primitives | `Surface`, `Section`, `KindPill`, `Timeline`, `TraceStrip` (`components/ui/`), `lib/format/glyphs.ts` (24 entity kinds → glyph + label + tone) | One vocabulary instead of per-page styling. Glyph is always paired with a text label. |
| Shell | Grouped navigation (Workspace / Explore / Intelligence), collapsible (persisted as a UI preference), glass side and top bars, "you are here" line (Personal / Project memory) | Navigation emphasises knowledge, not tables; adds **Projects**. |
| Graph | Glyph + text node labels, shape per type, type-tinted borders, dotted canvas, focus-dimming of non-neighbours on selection, zoom/fit controls, entity search-to-focus, "Knowledge now / as of …" strip stating the temporal scope, glyph legend, theme-change restyle | The graph should read as a knowledge map. Server budgets, truncation banner, list view and path finder are unchanged. |
| Graph panel | Selecting an entity opens a contextual glass panel: kind, name, aliases, actions, **Related knowledge**, **Recent history**, **Evidence** (all from the existing `GET /entities/{id}/context`) | Replaces the generic detail box. Nothing is invented. |
| Memory | Statement set as reading text; **trace strip** Memory → Evidence → Source → History; "how decided" and "why the AI remembers this" now come before entities and links; wording fixed for Git/agent-event memories | "Why does the AI remember this?" is first-class. |
| Overview | "Your second brain": recent memories, continue where you left off, projects, needs attention | Replaces three counters. |
| Projects (new) | `/projects` list; `/projects/[id]` (where it stands: current task, **explicitly recorded** unfinished work, decisions, bugs; activity timeline; sessions; files; commits); `/projects/[id]/sessions/[sid]` (what the agent did, noise like file inspection hidden); links into the project's own memory space | The project/coding-agent APIs existed but had no UI beyond the space switcher. 5 existing endpoints newly wrapped. |
| Ask | Evidence heading for cited memories, reading-type answers, note that time comes from the question | Citations still come only from the server's validated ids. |

## 2. Visual design decisions [I]

Calm, warm, knowledge-first; glass only on the side/top bars and graph panel; content surfaces are
solid. System font stacks only (no font download). The memory statement uses a serif reading stack.
I looked at light, dark and 390 px renderings of Overview, Graph, Memory and Project pages:
no horizontal overflow (asserted for Overview and Graph at 390 px) [V]. The graph screenshot had only
3 nodes, so how a dense graph looks with the new styling was **not** visually reviewed [L].

## 3. Interaction model

Nav collapse (`aria-expanded`, persisted in `localStorage`, works without storage); graph: click =
select + dim others, double-click/Expand = progressive neighbourhood, search = re-root, panel
links = focus on canvas if loaded else re-root; project pages link to "why remembered" which
selects the project's memory space first (`SpaceLink`; the server still checks ownership).

## 4. Accessibility

* Axe (serious/critical) passes on `/ /memories /entities /search /documents /review /settings
  /projects`, memory detail, entity page and graph states [V] (Playwright, default light scheme;
  the dark scheme was **not** run through axe [L]).
* Graph keeps its list-view alternative (default on narrow screens); canvas has a labelled zoom group.
* Type is never colour-only: glyph + text label + shape; temporal state by line style + text strip.
* `prefers-reduced-motion` disables animation/transition durations.
* Not done: resizable panels, command palette [L].

## 5. Security [V]

CSP spec, CSRF spec, HttpOnly-cookie test, project and user isolation specs pass unchanged. A
"project owned by another account is not found" E2E was added. No HTML injection (all backend text
rendered as text), no token in storage, only new storage is `nav-collapsed`.

## 6. Performance [M]

Same machine, same `e2e/perf.spec.ts`, pre-redesign commit (`7cdf8e1`) built in a scratch worktree vs
this build; n = 5; headless Chromium against a local production build.

| Metric | Before | After |
|---|---|---|
| `/memories` cold JS transferred (gzip) | 169,567 B | 170,458 B (+0.5 %) |
| `/memories` cold JS decoded | 553,940 B | 557,234 B |
| All static chunks, raw / gzip (3 new routes included) | 1436 KB / 437,449 B | 1488 KB / 452,931 B (+3.6 % / +3.5 %) |
| Graph first render, 150 nodes / 149 edges (median) | 620 ms | 640 ms |
| LCP of `/memories`, authenticated (median) | 172 ms | **296 ms** |

The LCP regression is real. An experiment with `backdrop-filter` removed gave 240 ms, so the glass blur
costs ≈ 56 ms of the +124 ms; the rest is a larger shell DOM and bigger content typography. All values
are far below a 2.5 s LCP target, so I kept the blur (it is a requested part of the look) but
note it. A page-level fade-in I tried made LCP worse and was removed. The graph render time was already
above Phase 10's 500 ms budget on this machine before the redesign (Phase 10 recorded 398 ms) [L].

## 7. Tests [V]

| Check | Result |
|---|---|
| Frontend Vitest | 106 passed (10 new: glyphs, graph labels, trace strip, space link, project overview incl. no-history / explicit-unfinished / not-found, session view hides noise) |
| `tsc --noEmit`, `eslint .` | clean |
| Playwright E2E on a production build | 46 passed (6 new: nav groups/collapse persistence, project pages end to end, cross-account project 404, memory trace, graph contextual panel); includes axe, CSP, CSRF, isolation |
| Backend regression subset (unit + graph API + projects + project memory + resume + auth), explicit test env | 1667 passed |
| Graph API conformance | 39/39; retrieval and document runners exit 0 |
| Not re-run | the full backend suite and the real-LLM / real-agent evaluations (backend unchanged by this phase) |

## 8. Limitations and remaining polish

* Documents, Search and Entity pages got the new tokens/panels but **no structural redesign** (their
  existing "passage → memory" and "why matched" content is unchanged).
* No timeline scrubber (the top-bar time lens remains the single control); no separate Timeline/Sources
  routes (memory evolution and provenance already live on the memory page).
* Ask citations open the memory (which shows its source); they do not link straight to the source.
* Project pages show what the existing resume/history/session endpoints return: no per-project
  "important entities" aggregate exists, so none is shown.
* The "Personal memory / <page>" line says "Project memory" for a selected project; it does not
  yet show the project's name.
* Screenshots were inspected but are not committed.
