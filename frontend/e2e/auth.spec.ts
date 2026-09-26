import { expect, test } from "@playwright/test";
import { login, seed } from "./helpers";

test("a protected page redirects to login and returns after sign-in", async ({ page }) => {
  await page.goto("/memories?status=active");
  await expect(page).toHaveURL(/\/login\?next=%2Fmemories%3Fstatus%3Dactive/);
  const s = seed();
  await page.getByLabel("Email").fill(s.a.email);
  await page.getByLabel("Password").fill(s.password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/memories\?status=active$/);
});

test("wrong password shows a generic error", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill(seed().a.email);
  await page.getByLabel("Password").fill("definitely the wrong password");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByText("Email or password is incorrect.")).toBeVisible();
});

test("the session is an HttpOnly cookie invisible to JavaScript; nothing in web storage", async ({ page, context }) => {
  await login(page);
  const cookie = (await context.cookies()).find((c) => c.name === "sb_session");
  expect(cookie?.httpOnly).toBe(true);
  expect(cookie?.sameSite).toBe("Lax");
  const visible = await page.evaluate(() => ({
    cookie: document.cookie,
    local: Object.keys(localStorage).length,
    session: Object.keys(sessionStorage).length,
  }));
  expect(visible).toEqual({ cookie: "", local: 0, session: 0 });
});

test("authenticated API responses are not cacheable", async ({ page }) => {
  await login(page);
  const res = await page.request.get("/api/memories/search");
  expect(res.status()).toBe(200);
  expect(res.headers()["cache-control"]).toBe("no-store");
});

test("a state-changing request from another origin is refused (CSRF)", async ({ page }) => {
  await login(page);
  const res = await page.request.post("/api/conversations", {
    data: { title: "csrf" },
    headers: { Origin: "https://evil.example" },
  });
  expect(res.status()).toBe(403);
});

test("a lost session sends the user to login with an expiry notice", async ({ page, context }) => {
  await login(page);
  await page.goto("/memories");
  await expect(page.getByRole("heading", { name: "Memories" })).toBeVisible();
  await context.clearCookies();
  // The next page that talks to the server discovers the lost session.
  await page.getByRole("link", { name: "Overview" }).click();
  await expect(page).toHaveURL(/\/login\?.*reason=expired/);
  await expect(page.getByText(/session ended/)).toBeVisible();
});

test("sign out ends the session server-side", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
  expect((await page.request.get("/api/auth/me")).status()).toBe(401);
  await page.goto("/memories");
  await expect(page).toHaveURL(/\/login/);
});
