import { expect, test } from "@playwright/test";
import { login, seed } from "./helpers";

test.beforeEach(async ({ page }) => login(page));

test("resolve a conflict: the result comes from the server", async ({ page }) => {
  await page.goto(`/review/memories/${seed().review.memory}`);
  await expect(page.getByText("Needs a decision")).toBeVisible();
  await page.getByRole("button", { name: "A replaces B" }).click();
  await expect(page.getByRole("dialog")).toContainText("will replace");
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText(/Decided: conflict resolved/)).toBeVisible();
  const res = await page.request.get(`/api/review/memories/${seed().review.memory}`);
  const body = await res.json();
  expect(body.status).toBe("conflict_resolved");
  expect(body.memory_b.memory.status).toBe("superseded");
});

test("approve an entity merge, then undo it", async ({ page }) => {
  await page.goto(`/review/entities/${seed().review.entity}`);
  await page.getByRole("button", { name: "Merge into OpenAI" }).click();
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Status: merged")).toBeVisible();
  await page.getByRole("button", { name: "Undo this merge" }).click();
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Status: reversed")).toBeVisible();
});
