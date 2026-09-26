import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against a REAL local stack (see README "End-to-end tests"):
 * the Next.js app (E2E_BASE_URL, default http://localhost:3100) proxying to a
 * backend in AUTH_MODE=session on a TEST database. `globalSetup` seeds two
 * throwaway accounts with scripts/e2e_seed.py; `globalTeardown` deletes them.
 * Serial: the tests share one seeded dataset and some make review decisions.
 */
export default defineConfig({
  testDir: "./e2e",
  // Performance measurements run only on request: E2E_PERF=1 E2E_HUB_SIZE=200.
  testIgnore: process.env.E2E_PERF === "1" ? [] : ["**/perf.spec.ts"],
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3100",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
