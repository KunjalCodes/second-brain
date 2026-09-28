import { describe, expect, it } from "vitest";
import { buildCsp } from "../../proxy";

function directive(csp: string, name: string): string {
  return csp.split("; ").find((d) => d.startsWith(`${name} `)) ?? "";
}

describe("buildCsp", () => {
  it("allows scripts only by nonce, with no 'unsafe-inline'", () => {
    const script = directive(buildCsp("abc123", false), "script-src");
    expect(script).toBe("script-src 'self' 'nonce-abc123' 'strict-dynamic'");
    expect(script).not.toContain("unsafe-inline");
  });

  it("adds 'unsafe-eval' in development only", () => {
    expect(directive(buildCsp("n", true), "script-src")).toContain("'unsafe-eval'");
    expect(directive(buildCsp("n", false), "script-src")).not.toContain("'unsafe-eval'");
  });

  it("keeps the other baseline directives", () => {
    const csp = buildCsp("n", false);
    for (const d of [
      "default-src 'self'",
      "connect-src 'self'",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
    ]) {
      expect(csp).toContain(d);
    }
  });
});
