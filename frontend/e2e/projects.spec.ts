import { expect, test } from "@playwright/test";
import { login } from "./helpers";

/**
 * Phase 16: a project is an isolated memory space. The Explorer's existing pages show it
 * through the memory-space switcher; nothing from one space is visible in another.
 */
test("switching memory space shows the project's history and never mixes it with personal memory", async ({ page, baseURL }) => {
  await login(page);
  // Cookie-authenticated writes need an allowed Origin (the CSRF boundary).
  const headers = { Origin: baseURL! };
  const created = await page.request.post("/api/projects", {
    headers,
    data: { name: "E2E Auth Service", repository_url: "https://github.com/e2e-acme/auth-service" },
  });
  expect(created.ok()).toBeTruthy();
  const projectId = (await created.json()).project.id as string;
  const stamp = new Date(Date.now() - 60_000).toISOString();
  const events = await page.request.post(`/api/projects/${projectId}/events`, {
    headers,
    data: {
      events: [
        { session_id: "e2e-s1", event_type: "session_started", timestamp: stamp, payload: { goal: "Fix the session timeout bug" } },
        { session_id: "e2e-s1", event_type: "decision", timestamp: stamp, payload: { title: "Extend the TTL instead", rationale: "Refreshing adds load", files: ["src/auth/session.py"] } },
      ],
    },
  });
  expect(events.ok()).toBeTruthy();

  await page.goto("/memories");
  await expect(page.getByText(/Drawing Extractor/).first()).toBeVisible();
  await expect(page.getByText("Extend the TTL instead")).toHaveCount(0);

  await page.getByLabel("Memory space").selectOption({ label: "Project: E2E Auth Service" });
  await expect(page.getByText(/Extend the TTL instead/).first()).toBeVisible();
  await expect(page.getByText(/Drawing Extractor/)).toHaveCount(0);

  // The project's entities are typed and exact (a file is `file:<path>`).
  await page.goto("/entities");
  await page.getByRole("searchbox").first().fill("session.py");
  await expect(page.getByText("file:src/auth/session.py").first()).toBeVisible();

  await page.getByLabel("Memory space").selectOption({ label: "Personal memory" });
  await page.goto("/memories");
  await expect(page.getByText(/Drawing Extractor/).first()).toBeVisible();
  await expect(page.getByText(/Extend the TTL instead/)).toHaveCount(0);
});
