/**
 * Performance MEASUREMENTS (not pass/fail budgets), run on request:
 *   E2E_PERF=1 E2E_HUB_SIZE=200 npx playwright test e2e/perf.spec.ts
 * Against whatever stack E2E_BASE_URL points at -- report the environment
 * with the numbers. Results are printed and written to test-results/perf.json.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { login, seed } from "./helpers";

const TRIALS = 5;
const results: Record<string, unknown> = {};

function stats(xs: number[]) {
  const s = [...xs].sort((a, b) => a - b);
  return { n: s.length, min: Math.round(s[0]!), median: Math.round(s[Math.floor(s.length / 2)]!), max: Math.round(s[s.length - 1]!) };
}

async function jsBytes(page: Page, path: string, ready: () => Promise<void>) {
  // Phase 11 (M10): split at the `load` event. JS requested before it is the
  // critical path; JS after it is Next.js prefetching the routes linked from
  // the navigation (idle-time, for faster later navigation). Phase 10 counted
  // both as "initial JS", which overstated the critical path by ~34 KB.
  let loaded = false;
  const onLoad = () => {
    loaded = true;
  };
  const sizes: { transferred: number; cached: boolean; decoded: number; critical: boolean }[] = [];
  const phase = new Map<string, boolean>();
  const onRequest = (r: import("@playwright/test").Request) => {
    if (r.url().includes("/_next/static/") && r.url().endsWith(".js")) phase.set(r.url(), !loaded);
  };
  const onResponse = async (r: import("@playwright/test").Response) => {
    if (r.url().includes("/_next/static/") && r.url().endsWith(".js")) {
      // Encoded (on-the-wire, gzip) body size vs the decoded JavaScript.
      // A browser-cache hit reports a non-positive size: nothing crossed the
      // wire, so it counts as 0 transferred (and as a cache hit).
      const { responseBodySize } = await r.request().sizes();
      sizes.push({ transferred: Math.max(0, responseBodySize), cached: responseBodySize <= 0, decoded: (await r.body()).length, critical: phase.get(r.url()) ?? true });
    }
  };
  page.on("load", onLoad);
  page.on("request", onRequest);
  page.on("response", onResponse);
  await page.goto(path);
  await ready();
  await page.waitForLoadState("networkidle");
  page.off("load", onLoad);
  page.off("request", onRequest);
  page.off("response", onResponse);
  const sum = (pick: (x: (typeof sizes)[number]) => number, filter: (x: (typeof sizes)[number]) => boolean) =>
    sizes.filter(filter).reduce((a, b) => a + pick(b), 0);
  return {
    requests: sizes.length,
    cache_hits: sizes.filter((x) => x.cached).length,
    critical_transferred_bytes: sum((x) => x.transferred, (x) => x.critical),
    prefetch_transferred_bytes: sum((x) => x.transferred, (x) => !x.critical),
    transferred_bytes: sum((x) => x.transferred, () => true),
    decoded_bytes: sum((x) => x.decoded, () => true),
  };
}

async function apiTiming(page: Page, url: string, method: "GET" | "POST" = "GET", data?: unknown) {
  const xs: number[] = [];
  for (let i = 0; i < TRIALS; i++) {
    const t = Date.now();
    const r = method === "GET" ? await page.request.get(url) : await page.request.post(url, { data, headers: { Origin: new URL(page.url()).origin } });
    expect(r.ok(), `${url} -> ${r.status()} ${(await r.text()).slice(0, 200)}`).toBe(true);
    xs.push(Date.now() - t);
  }
  return stats(xs);
}

test.describe.configure({ mode: "serial" });

test("page weight and auth bootstrap", async ({ page, browser }) => {
  results.login_js = await jsBytes(page, "/login", () => expect(page.getByRole("heading", { name: /Sign in/ })).toBeVisible());
  await login(page);
  results.memories_js = await jsBytes(page, "/memories", () => expect(page.getByRole("heading", { name: "Memories" })).toBeVisible());
  const boot: number[] = [];
  const state = await page.context().storageState();
  for (let i = 0; i < TRIALS; i++) {
    const ctx = await browser.newContext({ storageState: state });
    const p = await ctx.newPage();
    const t = Date.now();
    await p.goto("/memories");
    await expect(p.getByRole("heading", { name: "Memories" })).toBeVisible();
    boot.push(Date.now() - t);
    await ctx.close();
  }
  results.auth_bootstrap_to_first_page_ms = stats(boot);
});

async function lcp(page: Page): Promise<number> {
  return page.evaluate(
    () =>
      new Promise<number>((resolve) => {
        new PerformanceObserver((list) => {
          const entries = list.getEntries();
          resolve(entries[entries.length - 1]!.startTime);
        }).observe({ type: "largest-contentful-paint", buffered: true });
      }),
  );
}

test("cold cache: authenticated memories route JS and LCP", async ({ page, browser }) => {
  await login(page);
  const state = await page.context().storageState();
  const js: unknown[] = [];
  const lcps: number[] = [];
  const loginLcps: number[] = [];
  for (let i = 0; i < TRIALS; i++) {
    const ctx = await browser.newContext({ storageState: state });
    const p = await ctx.newPage();
    js.push(await jsBytes(p, "/memories", () => expect(p.getByRole("heading", { name: "Memories" })).toBeVisible()));
    lcps.push(await lcp(p));
    await ctx.close();
    const anon = await browser.newContext();
    const q = await anon.newPage();
    await q.goto("/login");
    await expect(q.getByRole("heading", { name: /Sign in/ })).toBeVisible();
    loginLcps.push(await lcp(q));
    await anon.close();
  }
  results.memories_js_cold = js[0];
  results.lcp_ms = { memories_authenticated: stats(lcps), login: stats(loginLcps) };
});

test("API latency through the proxy", async ({ page }) => {
  await login(page);
  const s = seed();
  results.api_ms = {
    memory_search: await apiTiming(page, "/api/memories/search?limit=25"),
    memory_detail: await apiTiming(page, `/api/memories/${s.memories.paddleocr}`),
    provenance: await apiTiming(page, `/api/memories/${s.memories.paddleocr}/provenance`),
    graph_overview: await apiTiming(page, "/api/graph/overview"),
    graph_neighborhood_2hop: await apiTiming(page, `/api/graph/entities/${s.entities.drawing_extractor}/neighborhood?hops=2`),
    documents: await apiTiming(page, "/api/documents"),
    hybrid_search: await apiTiming(page, "/api/search", "POST", { query: "OCR", limit: 50 }),
  };
});

test("graph canvas render at the server budget", async ({ page }) => {
  const s = seed();
  test.skip(!s.entities.hub, "run with E2E_HUB_SIZE>0");
  await login(page);
  const renders: { ms: number; nodes: string }[] = [];
  for (let i = 0; i < TRIALS; i++) {
    await page.goto(`/graph?focus=${s.entities.hub}`);
    const legend = page.getByText(/nodes · \d+ edges loaded · last render \d+ ms/);
    await expect(legend).toBeVisible({ timeout: 20_000 });
    const text = (await legend.textContent()) ?? "";
    renders.push({ ms: Number(/last render (\d+) ms/.exec(text)![1]), nodes: text.split(" · ").slice(0, 2).join(" · ") });
  }
  results.graph_canvas = {
    loaded: renders[0]!.nodes,
    truncation_banner_shown: await page.getByText(/stopped at its budget/).isVisible(),
    render_ms: stats(renders.map((r) => r.ms)),
  };
});

test.afterAll(() => {
  mkdirSync("test-results", { recursive: true });
  writeFileSync("test-results/perf.json", JSON.stringify(results, null, 2));
  console.log("PERF", JSON.stringify(results, null, 2));
});

/**
 * Phase 11 (M10): graph end-to-end attribution at ~50 / ~150 / ~300 nodes.
 * backend = the API's own Server-Timing; network = request time minus that;
 * layout = cose + element sync (canvas data-layout-ms); paint = two animation
 * frames after layout (data-paint-ms). Run with E2E_PERF_GRAPH=1.
 */
test("graph attribution: backend vs network vs layout vs paint", async ({ page }) => {
  const s = seed() as ReturnType<typeof seed> & { entities: Record<string, string | null> };
  test.skip(!s.entities.hub_a, "run with E2E_PERF_GRAPH=1");
  await login(page);

  async function timedSlice(action: () => Promise<unknown>) {
    const response = page.waitForResponse((r) => /\/api\/graph\/entities\/[^/]+\/neighborhood/.test(r.url()));
    await action();
    const r = await response;
    const body = await r.json(); // timing().responseEnd is only final once the body is read
    const t = r.request().timing();
    const total = t.responseEnd - t.requestStart;
    const server = Number(/app;dur=([\d.]+)/.exec(r.headers()["server-timing"] ?? "")?.[1] ?? NaN);
    return { total_ms: Math.round(total), backend_ms: Math.round(server), network_ms: Math.round(total - server), nodes: body.nodes.length, edges: body.edges.length, truncated: body.truncated_nodes };
  }

  async function canvasTimings() {
    const canvas = page.locator("[role=img][data-paint-ms]");
    await expect(canvas).toBeVisible({ timeout: 30_000 });
    return {
      elements: Number(await canvas.getAttribute("data-elements")),
      layout_ms: Number(await canvas.getAttribute("data-layout-ms")),
      paint_ms: Number(await canvas.getAttribute("data-paint-ms")),
    };
  }

  const cases: Record<string, unknown> = {};
  for (const [label, id] of [["~50 nodes", s.entities.small_hub], ["~150 nodes", s.entities.hub_a]] as const) {
    const runs = [];
    for (let i = 0; i < 3; i++) {
      const slice = await timedSlice(() => page.goto(`/graph?focus=${id}`));
      runs.push({ ...slice, ...(await canvasTimings()) });
    }
    cases[label] = runs;
  }

  // ~300 nodes / ~600 elements: expand Hub B from the list view, then show the canvas.
  const runs = [];
  for (let i = 0; i < 3; i++) {
    await page.goto(`/graph?focus=${s.entities.hub_a}`);
    await canvasTimings();
    await page.getByRole("button", { name: "List view" }).click();
    await page.getByRole("button", { name: "Perf Hub B", exact: true }).click();
    const slice = await timedSlice(() => page.getByRole("button", { name: "Expand neighbours" }).click());
    await page.getByRole("button", { name: "Canvas view" }).click();
    runs.push({ expansion: slice, ...(await canvasTimings()) });
  }
  cases["~300 nodes (after expansion; full canvas render)"] = runs;
  results.graph_attribution = cases;
});
