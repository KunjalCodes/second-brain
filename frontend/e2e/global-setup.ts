import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const SEED_FILE = resolve(here, ".auth/seed.json");
const BACKEND = resolve(here, "../../backend");

function seedScript(...args: string[]) {
  execFileSync("uv", ["run", "python", "../scripts/e2e_seed.py", ...args], { cwd: BACKEND, stdio: "inherit" });
}

/** Seeds two disposable accounts through the backend's own services (uv run). */
export default function globalSetup() {
  // A previous run kept its data (E2E_KEEP_DATA=1) or crashed: remove it first
  // so no seeded account is ever orphaned.
  if (existsSync(SEED_FILE)) seedScript("teardown", "--state", SEED_FILE);
  seedScript(
    "seed",
    "--out",
    SEED_FILE,
    "--hub-size",
    process.env.E2E_HUB_SIZE ?? "0",
    ...(process.env.E2E_PERF_GRAPH === "1" ? ["--perf-graph"] : []),
  );
}
