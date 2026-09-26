import { readFileSync } from "node:fs";
import { expect, type Page } from "@playwright/test";
import { SEED_FILE } from "./global-setup";

export interface Seed {
  password: string;
  a: { email: string; user_id: string };
  b: { email: string; user_id: string };
  memories: { easyocr: string; paddleocr: string; injected: string };
  entities: { drawing_extractor: string; hub: string | null };
  review: { memory: string; entity: string };
}

export function seed(): Seed {
  return JSON.parse(readFileSync(SEED_FILE, "utf8")) as Seed;
}

export async function login(page: Page, who: "a" | "b" = "a", next?: string) {
  const s = seed();
  await page.goto(next ? `/login?next=${encodeURIComponent(next)}` : "/login");
  await page.getByLabel("Email").fill(s[who].email);
  await page.getByLabel("Password").fill(s.password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).not.toHaveURL(/\/login/);
}
