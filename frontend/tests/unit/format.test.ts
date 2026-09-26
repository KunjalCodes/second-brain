import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api/client";
import type { MemoryStatus } from "@/lib/api/types";
import { localInputToIso, validityText } from "@/lib/format/dates";
import { documentErrorText, pollInterval } from "@/lib/format/documents";
import { DOCUMENT_STATUS, MEMORY_STATUS, TEMPORAL_STATE } from "@/lib/format/labels";
import { uploadErrorText } from "@/components/documents/DocumentList";
import { computeBars } from "@/components/temporal/MemoryTimeline";
import { channelsText } from "@/components/search/HybridSearch";
import { terminationNotice } from "@/components/ask/AskMemory";
import { passwordErrorText } from "@/components/settings/Settings";
import { loginErrorMessage } from "@/components/auth/LoginForm";
import { node, slice } from "./fixtures";

describe("status vocabulary", () => {
  const all: MemoryStatus[] = ["active", "conflicting", "historical", "superseded", "invalid", "archived", "merged"];
  it("labels every backend memory status distinctly, with an icon (not colour alone)", () => {
    const texts = all.map((s) => MEMORY_STATUS[s].text);
    expect(new Set(texts).size).toBe(all.length);
    for (const s of all) expect(MEMORY_STATUS[s].icon).toBeTruthy();
  });
  it("keeps the status axis separate from the temporal axis", () => {
    expect(MEMORY_STATUS.active.text).toBe("Active");
    expect(TEMPORAL_STATE.current.text).toBe("True now");
  });
  it("marks exactly the terminal document statuses", () => {
    expect(Object.entries(DOCUMENT_STATUS).filter(([, v]) => v.terminal).map(([k]) => k).sort()).toEqual(["complete", "failed", "partially_complete"]);
  });
});

describe("dates", () => {
  it("describes validity without inventing an end", () => {
    expect(validityText("2026-01-05T00:00:00Z", null)).toMatch(/^True since .* \(no known end\)$/);
    expect(validityText(null, null)).toBe("True with no recorded start or end");
  });
  it("sends a timezone-aware instant for the time lens", () => {
    expect(localInputToIso("2026-03-01T10:00")).toMatch(/Z$/);
    expect(localInputToIso("")).toBeNull();
    expect(localInputToIso("garbage")).toBeNull();
  });
});

describe("documents", () => {
  it("polls with bounded backoff and stops after 10 minutes", () => {
    expect(pollInterval(0)).toBe(3000);
    expect(pollInterval(61_000)).toBe(15_000);
    expect(pollInterval(10 * 60_000 + 1)).toBe(false);
  });
  it("maps server error codes and falls back to the server text", () => {
    expect(uploadErrorText(new ApiError(422, "document_rejected", "srv", { error_code: "no_extractable_text" }))).toBe(documentErrorText("no_extractable_text"));
    expect(uploadErrorText(new ApiError(429, "rate_limited", "x", undefined, null, 30))).toMatch(/30 seconds/);
  });
});

describe("computeBars", () => {
  it("keeps open-ended validity visibly open", () => {
    const bars = computeBars(slice({
      nodes: [
        node("old", { kind: "memory", valid_from: "2026-01-01T00:00:00Z", valid_until: "2026-03-01T00:00:00Z" }),
        node("new", { kind: "memory", valid_from: "2026-03-01T00:00:00Z", valid_until: null }),
      ],
    }));
    expect(bars.get("old")!.openEnd).toBe(false);
    expect(bars.get("new")!.openEnd).toBe(true);
    expect(bars.get("old")!.leftPct).toBe(0);
    expect(bars.get("new")!.leftPct).toBeGreaterThan(0);
  });
});

describe("text helpers", () => {
  it("explains retrieval channels in words", () => {
    expect(channelsText(["vector", "keyword"])).toBe("Matched by meaning (semantic similarity) + exact words (keyword)");
  });
  it("explains agent early stops, including unknown reasons", () => {
    expect(terminationNotice("final_answer")).toBeNull();
    expect(terminationNotice("budget_exhausted")).toMatch(/incomplete/);
    expect(terminationNotice("something_new")).toMatch(/something_new/);
  });
  it("distinguishes a wrong current password from other failures", () => {
    expect(passwordErrorText(new ApiError(401, "invalid_credentials", "x"))).toMatch(/current password is incorrect/);
    expect(passwordErrorText(new ApiError(422, "validation_error", "Password must be 12-128 characters long"))).toMatch(/12-128/);
  });
  it("never reveals whether an email exists on login", () => {
    expect(loginErrorMessage(new ApiError(401, "invalid_credentials", "x"))).not.toMatch(/not found|no account/i);
  });
});
