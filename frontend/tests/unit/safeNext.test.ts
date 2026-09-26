import { describe, expect, it } from "vitest";
import { safeNext } from "@/lib/auth/safeNext";

describe("safeNext (open-redirect guard)", () => {
  it.each([
    ["/memories", "/memories"],
    ["/memories/abc?status=active", "/memories/abc?status=active"],
    ["/graph?focus=x#y", "/graph?focus=x#y"],
  ])("keeps same-origin path %s", (raw, expected) => {
    expect(safeNext(raw)).toBe(expected);
  });

  it("keeps an encoded double slash as a same-origin path, never a host", () => {
    const out = safeNext("/%2F%2Fevil.example");
    expect(out.startsWith("//")).toBe(false);
    expect(new URL(out, "https://app.example").origin).toBe("https://app.example");
  });

  it.each([
    null,
    "",
    "https://evil.example/",
    "//evil.example/x",
    "/\\evil.example",
    "\\\\evil.example",
    "javascript:alert(1)",
    "memories",
    "/login",
    "/login?next=/x",
    "/\tevil",
  ])("rejects %s", (raw) => {
    expect(safeNext(raw as string | null)).toBe("/");
  });
});
