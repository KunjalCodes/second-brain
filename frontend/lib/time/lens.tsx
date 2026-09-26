"use client";
/**
 * The global time lens (docs/PHASE10_PLAN.md §8, ADR 0028): "Now" or "As of
 * <instant>". It only selects WHICH instant to ask the backend about; every
 * current/historical judgement is computed server-side. Held in memory only.
 */
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export interface TimeLens {
  /** null = now. Otherwise a timezone-aware ISO instant. */
  asOf: string | null;
  setAsOf: (iso: string | null) => void;
}

const LensContext = createContext<TimeLens | null>(null);

export function TimeLensProvider({ children }: { children: ReactNode }) {
  const [asOf, setAsOf] = useState<string | null>(null);
  const value = useMemo(() => ({ asOf, setAsOf }), [asOf]);
  return <LensContext.Provider value={value}>{children}</LensContext.Provider>;
}

export function useTimeLens(): TimeLens {
  const ctx = useContext(LensContext);
  if (!ctx) throw new Error("useTimeLens must be used inside <TimeLensProvider>");
  return ctx;
}
