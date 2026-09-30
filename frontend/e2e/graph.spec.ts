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

async function listView(page: import("@playwright/test").Page) {
  const toggle = page.getByRole("button", { name: /List view|Canvas view/ });
  if ((await toggle.textContent()) === "List view") await toggle.click();
}

test("only-no-longer-true mode shows the replaced relationship and hides current ones (Phase 15)", async ({ page }) => {
  await login(page);
  await page.goto(`/graph?focus=${seed().entities.drawing_extractor}`);
  await listView(page);
  await expect(page.getByRole("button", { name: /Drawing Extractor —uses→ PaddleOCR/ })).toBeVisible();
  await page.getByLabel("Only no-longer-true facts").check();
  await expect(page.getByRole("button", { name: /Drawing Extractor —uses→ EasyOCR/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Drawing Extractor —uses→ PaddleOCR/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Drawing Extractor —uses→ Gemini/ })).toHaveCount(0);
});

test("the relationship filter hides other predicates without refetching (Phase 15)", async ({ page }) => {
  await login(page);
  await page.goto(`/graph?focus=${seed().entities.drawing_extractor}`);
  await listView(page);
  await page.getByLabel("Include no-longer-true facts (labelled)").check();
  const relationship = page.getByRole("combobox", { name: /^Relationship/ });
  await expect(relationship).toBeVisible();
  await expect(relationship.locator("option")).toContainText(["all", "uses"]);
  await relationship.selectOption("uses");
  await expect(page.getByRole("button", { name: /Drawing Extractor —uses→ EasyOCR/ })).toBeVisible();
});

test("a relationship explains why it is here and why it is not current (Phase 15)", async ({ page }) => {
  await login(page);
  await page.goto(`/graph?focus=${seed().entities.drawing_extractor}`);
  await listView(page);
  await page.getByLabel("Include no-longer-true facts (labelled)").check();
  await page.getByRole("button", { name: /Drawing Extractor —uses→ EasyOCR/ }).click();
  const details = page.getByRole("complementary", { name: "Selection details" });
  const why = details.getByRole("region", { name: "Why this relationship exists" });
  await expect(why.getByText(/^No longer true:/)).toBeVisible();
  await expect(why.getByText("Not true now")).toBeVisible(); // temporally-derived basis
  await expect(why.getByText("Stated directly")).toBeVisible();
});

test("find a path between two entities: stored steps, labelled as inferred (Phase 15)", async ({ page }) => {
  await login(page);
  await page.goto(`/graph?focus=${seed().entities.drawing_extractor}`);
  await listView(page);
  await page.getByRole("button", { name: "PaddleOCR", exact: true }).click();
  await page.getByRole("button", { name: "Find path to…" }).click();
  await page.getByPlaceholder(/Connect to which entity/).fill("Gemini");
  const aside = page.getByRole("complementary", { name: "Selection details" });
  await aside.getByRole("button", { name: /Gemini/ }).click();
  const panel = page.getByRole("region", { name: "Path between entities" });
  await expect(panel.getByText("Inferred from the graph")).toBeVisible();
  await expect(panel.getByText("2 steps")).toBeVisible();
  await expect(panel.getByText("Drawing Extractor").first()).toBeVisible();
  await expect(panel.getByText(/heuristic/i)).toBeVisible();
});

test("entity page: at a glance, and memory page: how the knowledge evolved (Phase 15)", async ({ page }) => {
  await login(page);
  await page.goto(`/entities/${seed().entities.drawing_extractor}`);
  const glance = page.getByRole("region", { name: "At a glance" });
  await expect(glance).toBeVisible();
  await expect(glance.getByText("Most related entities")).toBeVisible();
  await expect(glance.getByText("PaddleOCR", { exact: true })).toBeVisible();

  await page.goto(`/memories/${seed().memories.easyocr}`);
  const evolution = page.getByRole("region", { name: "How this knowledge evolved" });
  await expect(evolution.getByText("First recorded", { exact: true })).toBeVisible();
  await expect(evolution.getByText("Replaced an earlier memory", { exact: true })).toBeVisible();
});
