import { expect, test } from "@playwright/test";
import { login, seed } from "./helpers";

/** Phase 18: the Obsidian-style workspace -- grouped navigation, project pages, the memory trace. */

test("navigation is grouped by purpose and can be collapsed; the choice persists", async ({ page }) => {
  await login(page);
  const nav = page.getByRole("navigation", { name: "Main" });
  for (const group of ["Workspace", "Explore", "Intelligence"]) await expect(nav.getByText(group, { exact: true })).toBeVisible();
  for (const link of ["Overview", "Memories", "Entities", "Graph", "Projects", "Documents", "Search", "Ask", "Review"]) {
    await expect(nav.getByRole("link", { name: link })).toBeVisible();
  }
  const toggle = page.getByRole("button", { name: "Collapse navigation" });
  await toggle.click();
  await expect(page.getByRole("button", { name: "Expand navigation" })).toHaveAttribute("aria-expanded", "false");
  await page.reload();
  await expect(page.getByRole("button", { name: "Expand navigation" })).toBeVisible();
  // Collapsed links keep their accessible names.
  await expect(nav.getByRole("link", { name: "Graph" })).toBeVisible();
  await page.getByRole("button", { name: "Expand navigation" }).click();
  await expect(page.getByRole("button", { name: "Collapse navigation" })).toBeVisible();
});

test("a project shows what stands, what the agent did, and links back to why it is remembered", async ({ page, baseURL }) => {
  await login(page);
  const headers = { Origin: baseURL! };
  const created = await page.request.post("/api/projects", {
    headers,
    data: { name: "E2E Workspace Project", repository_url: "https://github.com/e2e-acme/workspace-project" },
  });
  expect(created.ok()).toBeTruthy();
  const projectId = (await created.json()).project.id as string;
  const stamp = new Date(Date.now() - 60_000).toISOString();
  const sent = await page.request.post(`/api/projects/${projectId}/events`, {
    headers,
    data: {
      events: [
        { session_id: "ws-s1", event_type: "session_started", timestamp: stamp, payload: { goal: "Fix the login redirect" } },
        { session_id: "ws-s1", event_type: "decision", timestamp: stamp, payload: { title: "Redirect through the callback route", rationale: "Keeps state in one place", files: ["src/login.py"] } },
        { session_id: "ws-s1", event_type: "error", timestamp: stamp, payload: { message: "Redirect loop on logout" } },
      ],
    },
  });
  expect(sent.ok()).toBeTruthy();

  await page.goto("/projects");
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
  await page.getByRole("link", { name: "E2E Workspace Project" }).click();

  const standing = page.getByRole("region", { name: "Where this project stands" });
  await expect(standing.getByText("Redirect through the callback route")).toBeVisible();
  await expect(standing.getByText("Unfinished work")).toBeVisible();
  await expect(page.getByRole("region", { name: "Recent activity" })).toBeVisible();

  await page.getByRole("region", { name: "Agent sessions" }).getByRole("link").first().click();
  const did = page.getByRole("region", { name: "What the agent did" });
  await expect(did.getByText("Redirect through the callback route")).toBeVisible();
  await expect(did.getByText("Decision", { exact: true })).toBeVisible();

  // The memory behind the decision opens in the project's own memory space.
  await did.getByRole("link", { name: "why remembered" }).first().click();
  await expect(page.getByRole("navigation", { name: "Trace" })).toBeVisible();
  await expect(page.getByText("Project memory")).toBeVisible();
});

test("a project another account owns is not found", async ({ page, baseURL }) => {
  await login(page, "b");
  const created = await page.request.post("/api/projects", {
    headers: { Origin: baseURL! },
    data: { name: "B Private Project", repository_url: "https://github.com/e2e-b/private" },
  });
  const projectId = (await created.json()).project.id as string;
  await page.context().clearCookies();
  await login(page, "a");
  await page.goto(`/projects/${projectId}`);
  await expect(page.getByText("Not found")).toBeVisible();
  await page.goto("/projects");
  await expect(page.getByText("B Private Project")).toHaveCount(0);
});

test("a memory shows its trace: memory, evidence, source, history", async ({ page }) => {
  await login(page);
  await page.goto(`/memories/${seed().memories.easyocr}`);
  const trace = page.getByRole("navigation", { name: "Trace" });
  for (const step of ["Memory", "Evidence", "Source", "History"]) await expect(trace.getByRole("link", { name: step })).toBeVisible();
  await expect(page.getByRole("region", { name: "How this memory was decided" })).toBeVisible();
});

test("selecting an entity in the graph opens a contextual panel with related knowledge", async ({ page }) => {
  await login(page);
  await page.goto(`/graph?focus=${seed().entities.drawing_extractor}`);
  const toggle = page.getByRole("button", { name: /List view|Canvas view/ });
  if ((await toggle.textContent()) === "List view") await toggle.click();
  await page.getByRole("button", { name: "PaddleOCR", exact: true }).click();
  const panel = page.getByRole("complementary", { name: "Selection details" });
  await expect(panel.getByText("Related knowledge")).toBeVisible();
  await expect(panel.getByText("Recent history")).toBeVisible();
  await expect(panel.getByText("Technology")).toBeVisible();
  // The time state is stated, not implied.
  await expect(page.getByRole("status").filter({ hasText: "Knowledge now" })).toBeVisible();
});
