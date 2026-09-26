import { expect, test } from "@playwright/test";
import { login } from "./helpers";

test("upload a text document and see it listed with its processing status", async ({ page }) => {
  await login(page);
  await page.goto("/documents");
  await page.getByLabel("Choose a document").setInputFiles({
    name: "e2e-notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Atlas uses Postgres for storage.\n<script>window.__pwned=1</script>\n"),
  });
  await expect(page.getByRole("status").getByRole("link", { name: "e2e-notes.txt" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("listitem").getByRole("link", { name: "e2e-notes.txt" })).toBeVisible();
});

test("an unsupported file type is refused by the server with a clear message", async ({ page }) => {
  await login(page);
  await page.goto("/documents");
  await page.getByLabel("Choose a document").setInputFiles({ name: "x.exe", mimeType: "application/octet-stream", buffer: Buffer.from("MZ") });
  await expect(page.getByRole("alert")).toBeVisible();
});
