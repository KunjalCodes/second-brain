import { expect, test, type Page } from "@playwright/test";
import { login } from "./helpers";

/** Phase 11 (M12): the production CSP is strict and per request. */

function scriptSrc(csp: string): string {
  return csp.split(";").map((d) => d.trim()).find((d) => d.startsWith("script-src")) ?? "";
}

test("documents carry a strict, per-request script policy", async ({ page }) => {
  const first = await page.request.get("/login");
  const second = await page.request.get("/login");
  const a = first.headers()["content-security-policy"] ?? "";
  const b = second.headers()["content-security-policy"] ?? "";

  const src = scriptSrc(a);
  expect(src).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
  expect(src).toContain("'strict-dynamic'");
  expect(src).not.toContain("'unsafe-inline'");
  expect(src).not.toContain("'unsafe-eval'"); // production build
  expect(a).toContain("frame-ancestors 'none'");
  expect(a).toContain("object-src 'none'");

  const nonceA = /'nonce-([^']+)'/.exec(a)?.[1];
  const nonceB = /'nonce-([^']+)'/.exec(b)?.[1];
  expect(nonceA).toBeTruthy();
  expect(nonceA).not.toBe(nonceB);

  // Every script the server sends carries this response's nonce.
  const html = await first.text();
  const tags = html.match(/<script\b[^>]*>/g) ?? [];
  expect(tags.length).toBeGreaterThan(0);
  for (const tag of tags) expect(tag).toContain(`nonce="${nonceA}"`);
});

async function violations(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __cspViolations: string[] }).__cspViolations);
}

test("the app runs under the policy with no violations", async ({ page }) => {
  await page.addInitScript(() => {
    const w = window as unknown as { __cspViolations: string[] };
    w.__cspViolations = [];
    document.addEventListener("securitypolicyviolation", (e) => {
      w.__cspViolations.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  await login(page);
  for (const path of ["/memories", "/graph", "/search", "/documents", "/review", "/settings"]) {
    await page.goto(path);
    await expect(page.locator("main")).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(await violations(page), path).toEqual([]);
  }
});
