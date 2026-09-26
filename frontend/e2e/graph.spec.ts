import { expect, test } from "@playwright/test";
import { login, seed } from "./helpers";

test("graph neighbourhood, list view, expand and edge evidence", async ({ page }) => {
  await login(page);
  await page.goto(`/graph?focus=${seed().entities.drawing_extractor}`);
  await expect(page.getByRole("heading", { name: "Graph — neighbourhood" })).toBeVisible();
  const toggle = page.getByRole("button", { name: /List view|Canvas view/ });
  if ((await toggle.textContent()) === "List view") await toggle.click();
  await expect(page.getByRole("button", { name: "PaddleOCR", exact: true })).toBeVisible();
  // Current scope: the superseded EasyOCR relationship is not shown as current.
  await expect(page.getByRole("button", { name: /Drawing Extractor —uses→ EasyOCR/ })).toHaveCount(0);

  await page.getByRole("button", { name: /Drawing Extractor —uses→ PaddleOCR/ }).click();
  const details = page.getByRole("complementary", { name: "Selection details" });
  await expect(details.getByText(/switched from EasyOCR to PaddleOCR/)).toBeVisible();

  await page.getByLabel("Include no-longer-true facts (labelled)").check();
  await expect(page.getByRole("button", { name: /Drawing Extractor —uses→ EasyOCR/ })).toBeVisible();
});
