import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { login, seed } from "./helpers";

test("login page has no serious accessibility violations", async ({ page }) => {
  await page.goto("/login");
  const r = await new AxeBuilder({ page }).analyze();
  expect(r.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
});

test.describe("signed in", () => {
  test.beforeEach(async ({ page }) => login(page));
  for (const path of ["/", "/memories", "/entities", "/search", "/documents", "/review", "/settings"]) {
    test(`${path} has no serious accessibility violations`, async ({ page }) => {
      await page.goto(path);
      await page.waitForLoadState("networkidle");
      const r = await new AxeBuilder({ page }).analyze();
      expect(r.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => `${v.id}: ${v.nodes[0]?.target}`)).toEqual([]);
    });
  }

  test("memory detail has no serious accessibility violations", async ({ page }) => {
    await page.goto(`/memories/${seed().memories.easyocr}`);
    await page.waitForLoadState("networkidle");
    const r = await new AxeBuilder({ page }).analyze();
    expect(r.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
  });

  test("keyboard: the skip link is first and moves focus to the content", async ({ page }) => {
    await page.goto("/memories");
    await expect(page.getByRole("heading", { name: "Memories" })).toBeVisible();
    await page.keyboard.press("Tab");
    // `next dev` injects its dev-tools portal first in the tab order; it does not exist in a production build.
    if (await page.evaluate(() => document.activeElement?.tagName === "NEXTJS-PORTAL")) await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/#main$/);
  });

  test("phone width: no horizontal scroll; the graph opens as a list", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 });
    for (const path of ["/", "/memories", `/memories/${seed().memories.easyocr}`, "/documents"]) {
      await page.goto(path);
      await page.waitForLoadState("networkidle");
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, path).toBeLessThanOrEqual(0);
    }
    await page.goto(`/graph?focus=${seed().entities.drawing_extractor}`);
    await expect(page.getByRole("button", { name: "Canvas view" })).toBeVisible();
  });
});
