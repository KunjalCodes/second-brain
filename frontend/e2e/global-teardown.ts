import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
import { SEED_FILE } from "./global-setup";

export default function globalTeardown() {
  if (!existsSync(SEED_FILE) || process.env.E2E_KEEP_DATA === "1") return;
  execFileSync("uv", ["run", "python", "../scripts/e2e_seed.py", "teardown", "--state", SEED_FILE], {
    cwd: resolve(here, "../../backend"),
    stdio: "inherit",
  });
}
