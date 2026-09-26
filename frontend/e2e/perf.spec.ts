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
  const sizes: { transferred: number; decoded: number }[] = [];
  const onResponse = async (r: import("@playwright/test").Response) => {
    if (r.url().includes("/_next/static/") && r.url().endsWith(".js")) {
      // Encoded (on-the-wire, gzip) body size vs the decoded JavaScript.
      const { responseBodySize } = await r.request().sizes();
      sizes.push({ transferred: responseBodySize, decoded: (await r.body()).length });
    }
  };
  page.on("response", onResponse);
  await page.goto(path);
  await ready();
  await page.waitForLoadState("networkidle");
  page.off("response", onResponse);
  return {
    requests: sizes.length,
    transferred_bytes: sizes.reduce((a, b) => a + b.transferred, 0),
    decoded_bytes: sizes.reduce((a, b) => a + b.decoded, 0),
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
