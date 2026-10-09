import "./navigation.mock";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setTransport } from "@/lib/api/client";
import { getMemorySpace, resetMemorySpaceForTests } from "@/lib/api/space";
import { kindOf, ENTITY_KIND } from "@/lib/format/glyphs";
import { eventStyle, NOISY_EVENT_TYPES } from "@/lib/format/projectEvents";
import { emptyGraph, mergeSlice, toElements } from "@/lib/graph/model";
import { ProjectOverview } from "@/components/projects/ProjectOverview";
import { SessionView } from "@/components/projects/SessionView";
import { SpaceLink } from "@/components/projects/SpaceLink";
import { TraceStrip } from "@/components/ui/Timeline";
import { fakeTransport, json, node, slice } from "./fixtures";
import { renderWithClient } from "./render";

const PID = "11111111-1111-4111-8111-111111111111";
const ref = (id: string, type = "agent_event") => ({ id, type });

beforeEach(() => {
  window.localStorage.clear();
  resetMemorySpaceForTests();
});
afterEach(() => setTransport(null));

describe("kinds of knowledge", () => {
  it("gives every kind a glyph AND a text label, and falls back safely", () => {
    for (const k of Object.values(ENTITY_KIND)) {
      expect(k.glyph.length).toBeGreaterThan(0);
      expect(k.label.length).toBeGreaterThan(0);
    }
    expect(kindOf("entity", "decision").label).toBe("Decision");
    expect(kindOf("memory").label).toBe("Memory");
    expect(kindOf("entity", "something_new").label).toBe("Other");
  });

  it("puts the glyph and the text into the graph label and keeps the plain label", () => {
    const g = mergeSlice(emptyGraph(), slice({ nodes: [node("a", { label: "Payments API", entity_type: "project" })] }), true);
    const el = toElements(g).find((e) => e.data.id === "a")!;
    expect(el.data.display).toBe("◆ Payments API");
    expect(el.data.label).toBe("Payments API");
    expect(el.data.entity_type).toBe("project");
  });

  it("styles known agent events and humanises unknown ones", () => {
    expect(eventStyle("decision").label).toBe("Decision");
    expect(eventStyle("brand_new_event").label).toBe("brand new event");
    expect(NOISY_EVENT_TYPES.has("file_inspected")).toBe(true);
  });
});

describe("TraceStrip", () => {
  it("renders the memory -> evidence -> source -> history path as links", () => {
    renderWithClient(<TraceStrip steps={[{ label: "Memory", href: "#a" }, { label: "Evidence", href: "#b" }]} />);
    expect(screen.getByRole("navigation", { name: "Trace" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Evidence" }).getAttribute("href")).toBe("#b");
  });
});

describe("SpaceLink", () => {
  it("selects the project's memory space before following the link", () => {
    renderWithClient(<SpaceLink projectId={PID} href="/graph">Open</SpaceLink>);
    expect(getMemorySpace()).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "Open" }));
    expect(getMemorySpace()).toBe(PID);
  });
});

const resume = (extra: object = {}) => ({
  active_task: null, attempts: [], files_inspected: [], files_modified: ["src/a.py"], generated_at: "2026-06-01T00:00:00Z",
  has_history: true, important_memories: [], known_bugs: [], last_session: null, limits: {}, notice: "n", previous_errors: [],
  previous_sessions: [], project: {}, ranking: [], recent_commits: [{ sha: "abcdef1234567890", subject: "Fix redirect", committed_at: "2026-06-01T00:00:00Z", evidence: "git", source: ref("abcdef1234567890", "git_commit") }],
  recent_decisions: [{ title: "Use the callback route", rationale: "One place for state", files: [], occurred_at: "2026-06-01T00:00:00Z", relevance: [], session_id: "s1", source: ref("e1"), status: "active", superseded_by: null }],
  relevant_sources: [], tests_run: [], unfinished_work: [], ...extra,
});

describe("ProjectOverview", () => {
  const routes = (r: object) => (url: string) => {
    if (url.includes("/resume")) return json(200, r);
    if (url.includes("/history")) return json(200, { items: [
      { kind: "event", type: "decision", title: "Use the callback route", occurred_at: "2026-06-01T00:00:00Z", recorded_at: "2026-06-01T00:00:00Z", evidence: "agent_stated", session_id: "s1", memory_id: "m1", source: ref("e1") },
      { kind: "event", type: "file_inspected", title: "NOISE", occurred_at: "2026-06-01T00:00:00Z", recorded_at: "2026-06-01T00:00:00Z", evidence: "agent_stated", source: ref("e2") },
    ], notice: "n" });
    if (url.includes("/sessions")) return json(200, { sessions: [{ session_id: "s1", agent_name: null, started_at: "2026-06-01T00:00:00Z", last_event_at: "2026-06-01T00:00:00Z", ended_at: null, event_count: 3, outcome: null, status: "active", summary: "Fix the redirect" }], notice: "n" });
    return json(200, { id: PID, name: "Payments API", identity_kind: "remote_url", identity_fingerprint: "x", default_branch: "main", status: "active", created_at: "2026-01-01T00:00:00Z" });
  };

  it("shows decisions, commits, sessions and activity from the API, without raw-event noise", async () => {
    setTransport(fakeTransport(routes(resume())).transport);
    renderWithClient(<ProjectOverview id={PID} />);
    expect(await screen.findByRole("heading", { name: "Payments API" })).toBeTruthy();
    expect((await screen.findAllByText(/Use the callback route/)).length).toBeGreaterThan(0);
    expect(await screen.findByText(/Fix redirect/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Fix the redirect" }).getAttribute("href")).toBe(`/projects/${PID}/sessions/s1`);
    expect(screen.queryByText("NOISE")).toBeNull();
    // Never invents unfinished work.
    expect(await screen.findByText(/Nothing is recorded as unfinished/)).toBeTruthy();
  });

  it("says so, and invents nothing, when a project has no history", async () => {
    setTransport(fakeTransport(routes(resume({ has_history: false, recent_decisions: [], recent_commits: [] }))).transport);
    renderWithClient(<ProjectOverview id={PID} />);
    expect(await screen.findByText("No history recorded yet")).toBeTruthy();
  });

  it("lists explicitly recorded unfinished work with its basis", async () => {
    const u = [{ basis: "task_blocked", occurred_at: "2026-06-01T00:00:00Z", session_id: "s1", text: "Waiting on staging" }];
    setTransport(fakeTransport(routes(resume({ unfinished_work: u }))).transport);
    renderWithClient(<ProjectOverview id={PID} />);
    expect(await screen.findByText("Waiting on staging")).toBeTruthy();
    expect(screen.getByText(/recorded as blocked/)).toBeTruthy();
  });

  it("shows a not-found state for a project that is not yours", async () => {
    setTransport(fakeTransport(() => json(404, { error: { code: "not_found", message: "Project not found", request_id: "r" } })).transport);
    renderWithClient(<ProjectOverview id={PID} />);
    await waitFor(() => expect(screen.getByText("Not found")).toBeTruthy());
  });
});

describe("SessionView", () => {
  it("tells the session's story and hides inspection noise", async () => {
    const ev = (type: string, title: string, id: string) => ({ kind: "event", type, title, occurred_at: "2026-06-01T00:00:00Z", recorded_at: "2026-06-01T00:00:00Z", evidence: "agent_stated", memory_id: null, source: ref(id) });
    setTransport(fakeTransport(() => json(200, {
      commits: [], continues_session: null, counts: {}, decisions: ["Use the callback route"], errors: ["Redirect loop"], events: [
        ev("user_request", "Fix the redirect", "a"), ev("file_inspected", "src/x.py", "b"), ev("error", "Redirect loop", "c"), ev("fix", "Clear the cookie", "d"),
      ], files_inspected: [], files_modified: ["src/login.py"], fixes: [], goal: "Fix the redirect", next_after_seq: null, notice: "n",
      requests: ["Fix the redirect"], session: { session_id: "s1", agent_name: null, started_at: "2026-06-01T00:00:00Z", last_event_at: "2026-06-01T00:00:00Z", ended_at: null, event_count: 4, outcome: null, status: "active", summary: null },
      tests: [], unfinished: [],
    })).transport);
    renderWithClient(<SessionView projectId={PID} sessionId="s1" />);
    expect(await screen.findByText("Clear the cookie")).toBeTruthy();
    expect(screen.getAllByText("Error").length).toBeGreaterThan(0);
    expect(screen.queryByText("src/x.py")).toBeNull();
    expect(screen.getByText("src/login.py")).toBeTruthy();
  });
});
