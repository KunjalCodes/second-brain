/**
 * Validates the post-login redirect target (open-redirect guard,
 * docs/PHASE10_PLAN.md §14). Only same-origin RELATIVE paths are allowed:
 * must start with a single "/", not "//" or "/\", no scheme, no control chars.
 */
export function safeNext(raw: string | null | undefined, fallback = "/"): string {
  if (!raw) return fallback;
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return fallback;
  if (/[\u0000-\u001f\u007f]/.test(raw)) return fallback;
  if (raw.startsWith("/login") || raw.startsWith("/register")) return fallback;
  try {
    const url = new URL(raw, "http://same-origin.invalid");
    if (url.origin !== "http://same-origin.invalid") return fallback;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return fallback;
  }
}

/** Frontend build/config flag (Q11): the backend disables registration in production. */
export const REGISTRATION_ENABLED = process.env.NEXT_PUBLIC_REGISTRATION_ENABLED === "true";
