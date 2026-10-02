import "./navigation.mock";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { apiRequest, setTransport } from "@/lib/api/client";
import { getMemorySpace, resetMemorySpaceForTests, setMemorySpace } from "@/lib/api/space";
import { ProjectSwitcher } from "@/components/layout/ProjectSwitcher";
import { emptyGraph, mergeSlice, toElements } from "@/lib/graph/model";
import { fakeTransport, json, node, slice } from "./fixtures";
import { renderWithClient } from "./render";

const P1 = "11111111-1111-4111-8111-111111111111";
const P2 = "22222222-2222-4222-8222-222222222222";
const project = (id: string, name: string) => ({
  id, name, identity_kind: "remote_url", identity_fingerprint: "abc", default_branch: null,
  status: "active", created_at: "2026-01-01T00:00:00Z",
});

beforeEach(() => {
  window.localStorage.clear();
  resetMemorySpaceForTests();
});
afterEach(() => setTransport(null));

describe("memory space selection", () => {
  it("sends X-Memory-Space on data routes only, never on auth or project routes", async () => {
    const { transport, calls } = fakeTransport(() => json(200, {}));
    setTransport(transport);
    setMemorySpace(P1);
    await apiRequest("/memories/search");
    await apiRequest("/auth/me");
    await apiRequest("/projects");
    expect((calls[0]!.init.headers as Record<string, string>)["X-Memory-Space"]).toBe(P1);
    expect((calls[1]!.init.headers as Record<string, string>)["X-Memory-Space"]).toBeUndefined();
    expect((calls[2]!.init.headers as Record<string, string>)["X-Memory-Space"]).toBeUndefined();
  });

  it("sends nothing in personal memory", async () => {
    const { transport, calls } = fakeTransport(() => json(200, {}));
    setTransport(transport);
    await apiRequest("/memories/search");
    expect((calls[0]!.init.headers as Record<string, string>)["X-Memory-Space"]).toBeUndefined();
  });

  it("only accepts a UUID, so arbitrary text can never become a header value", () => {
    setMemorySpace("x\r\nX-Evil: 1");
    expect(getMemorySpace()).toBeNull();
    setMemorySpace(P2);
    expect(getMemorySpace()).toBe(P2);
    setMemorySpace(null);
    expect(getMemorySpace()).toBeNull();
  });

  it("restores a stored selection and ignores a corrupt one", () => {
    window.localStorage.setItem("memory-space", P1);
    resetMemorySpaceForTests();
    expect(getMemorySpace()).toBe(P1);
    window.localStorage.setItem("memory-space", "<script>");
    resetMemorySpaceForTests();
    expect(getMemorySpace()).toBeNull();
  });
});

describe("ProjectSwitcher", () => {
  it("lists personal memory and the projects, and switching clears the cache", async () => {
    const { transport } = fakeTransport(() => json(200, { items: [project(P1, "Auth"), project(P2, "Billing")], limit: 100, offset: 0 }));
    setTransport(transport);
    const { client } = renderWithClient(<ProjectSwitcher />);
    const select = (await screen.findByLabelText("Memory space")) as HTMLSelectElement;
    await waitFor(() => expect(select.disabled).toBe(false));
    expect([...select.options].map((o) => o.text)).toEqual(["Personal memory", "Project: Auth", "Project: Billing"]);
    client.setQueryData(["memories", "x"], { stale: true });
    fireEvent.change(select, { target: { value: P2 } });
    expect(getMemorySpace()).toBe(P2);
    expect(client.getQueryData(["memories", "x"])).toBeUndefined();
  });

  it("renders nothing for an account with no projects", async () => {
    const { transport } = fakeTransport(() => json(200, { items: [], limit: 100, offset: 0 }));
    setTransport(transport);
    const { container } = renderWithClient(<ProjectSwitcher />);
    await waitFor(() => expect(container.querySelector("select")).toBeNull());
  });

  it("falls back to personal memory when the stored project no longer exists", async () => {
    setMemorySpace(P1);
    const { transport } = fakeTransport(() => json(200, { items: [project(P2, "Billing")], limit: 100, offset: 0 }));
    setTransport(transport);
    renderWithClient(<ProjectSwitcher />);
    await waitFor(() => expect(getMemorySpace()).toBeNull());
  });

  it("shows project names as text, never markup", async () => {
    const { transport } = fakeTransport(() => json(200, { items: [project(P1, '<img src=x onerror="window.__pwned=1">')], limit: 100, offset: 0 }));
    setTransport(transport);
    renderWithClient(<ProjectSwitcher />);
    await screen.findByLabelText("Memory space");
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("graph model for project entity types", () => {
  it("carries entity_type into the renderer so shape can differ by type", () => {
    const g = mergeSlice(emptyGraph(), slice({ nodes: [node("f", { entity_type: "file" }), node("c", { entity_type: "commit" }), node("m", { kind: "memory" })] }));
    const types = Object.fromEntries(toElements(g).map((e) => [e.data.id, e.data.entity_type]));
    expect(types).toEqual({ f: "file", c: "commit", m: "" });
  });
});
