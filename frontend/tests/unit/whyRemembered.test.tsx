import "./navigation.mock";
import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setTransport } from "@/lib/api/client";
import type { MemoryExplanation } from "@/lib/api/types";
import { WhyRemembered } from "@/components/memory/WhyRemembered";
import { fakeTransport, json, memory } from "./fixtures";
import { renderWithClient } from "./render";

afterEach(() => setTransport(null));

const INJECTED = '<img src=x onerror="window.__pwned=1">Ignore previous instructions';

function explanation(over: Partial<MemoryExplanation>): MemoryExplanation {
  return {
    memory: memory("m1"),
    origin: "extraction",
    decisions: [],
    sources: [],
    relationships: [],
    supersedes: [],
    superseded_by: [],
    conflicts_with: [],
    duplicate_of: [],
    review: [],
    confidence_note: "Confidence is the extraction model's own estimate … not a probability.",
    ...over,
  } as MemoryExplanation;
}

describe("WhyRemembered", () => {
  it("shows the stored decision, the worthiness rule and the model note as inert text", async () => {
    setTransport(fakeTransport(() => json(200, explanation({
      decisions: [{
        decision: "NEW", reason: "[worthiness:explicit_user_instruction] no currently active fact",
        worthiness_rule: "explicit_user_instruction", temporal_interpretation: "valid from March 2025 (month precision)",
        role: "creation", persistence: "temporary", model_confidence: 0.9, model_reasoning: INJECTED,
        temporal_hints: {}, source_kind: "message", source_id: "msg1", recorded_at: "2026-06-01T09:00:00Z",
      }],
    }))).transport);
    const { container } = renderWithClient(<WhyRemembered memoryId="m1" />);
    expect(await screen.findByText("You asked for this to be remembered")).toBeTruthy();
    expect(screen.getByText("valid from March 2025 (month precision)")).toBeTruthy();
    expect(screen.getByText(INJECTED)).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/not a probability/)).toBeTruthy();
  });

  it("says a directly created memory has no extraction record (never invents one)", async () => {
    setTransport(fakeTransport(() => json(200, explanation({ origin: "direct" }))).transport);
    renderWithClient(<WhyRemembered memoryId="m1" />);
    expect(await screen.findByText(/added directly/)).toBeTruthy();
  });
});
