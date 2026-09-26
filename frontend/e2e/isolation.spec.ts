import { expect, test } from "@playwright/test";
import { login, seed } from "./helpers";

test("another user cannot see user A's memory, entity or review item", async ({ page }) => {
  await login(page, "b");
  const s = seed();
  for (const path of [`/memories/${s.memories.paddleocr}`, `/review/memories/${s.review.memory}`]) {
    await page.goto(path);
    await expect(page.getByText(/doesn’t exist, or it isn’t yours|doesn't exist, or it isn't yours/)).toBeVisible();
  }
  expect((await page.request.get(`/api/memories/${s.memories.paddleocr}`)).status()).toBe(404);
  expect((await page.request.get(`/api/entities/${s.entities.drawing_extractor}`)).status()).toBe(404);
  const list = await (await page.request.get("/api/memories/search")).json();
  expect(list.total).toBe(0);
});
