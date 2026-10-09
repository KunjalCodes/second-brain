"use client";
/**
 * Explicit screen states (docs/PHASE10_PLAN.md §19): no blank screens, no
 * unbounded spinners. Errors carry the backend request id for correlation.
 */
import { useEffect, useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api/client";
import { Button } from "./Button";

export function LoadingRows({ rows = 5, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-label={label} className="space-y-2">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton h-12" />
      ))}
      <span className="sr-only">{label}…</span>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-[var(--radius-panel)] border border-dashed border-[var(--color-border)] p-8 text-center">
      <p className="font-medium">{title}</p>
      {children ? <div className="mt-2 text-sm text-[var(--color-fg-muted)]">{children}</div> : null}
    </div>
  );
}

function useCountdown(seconds: number | null): number | null {
  const [left, setLeft] = useState(seconds);
  const [prev, setPrev] = useState(seconds);
  if (prev !== seconds) {
    setPrev(seconds);
    setLeft(seconds);
  }
  useEffect(() => {
    if (seconds === null) return;
    const id = setInterval(() => setLeft((s) => (s !== null && s > 0 ? s - 1 : 0)), 1000);
    return () => clearInterval(id);
  }, [seconds]);
  return left;
}

export function describeError(error: unknown, notFound?: string): { title: string; body: string } {
  if (!(error instanceof ApiError)) return { title: "Something went wrong", body: "An unexpected error occurred." };
  if (error.isNetwork)
    return {
      title: error.code === "timeout" ? "The server is taking too long" : "Can't reach the server",
      body:
        error.code === "timeout"
          ? "It may be starting up. Try again in a moment."
          : "Check your connection and try again.",
    };
  switch (error.status) {
    case 403:
      return error.code === "origin_not_allowed"
        ? { title: "Configuration problem", body: "The server refused this request's origin. The deployment's FRONTEND_URL is probably misconfigured." }
        : { title: "Not allowed", body: error.message };
    case 404:
      return { title: "Not found", body: notFound ?? "This doesn't exist, or it isn't yours." };
    case 409:
      return { title: "This changed since you opened it", body: error.message };
    case 422:
      return { title: "The request wasn't accepted", body: error.message };
    case 429:
      return { title: "Too many requests", body: "You've hit a rate limit." };
    default:
      return error.status >= 500
        ? { title: "The service is unavailable", body: "The server had a problem. Try again shortly." }
        : { title: "Request failed", body: error.message };
  }
}

export function ErrorState({ error, onRetry, notFound }: { error: unknown; onRetry?: () => void; notFound?: string }) {
  const api = error instanceof ApiError ? error : null;
  const left = useCountdown(api?.status === 429 ? api.retryAfter : null);
  const { title, body } = describeError(error, notFound);
  const canRetry = onRetry && api?.status !== 404 && api?.status !== 403;
  return (
    <div role="alert" className="panel border border-[var(--color-danger)] p-4">
      <p className="font-medium text-[var(--color-danger)]">{title}</p>
      <p className="mt-1 text-sm">{body}</p>
      {api?.status === 429 && left !== null ? (
        <p className="mt-1 text-sm">{left > 0 ? `Try again in ${left}s.` : "You can try again now."}</p>
      ) : null}
      {api?.requestId ? (
        <p className="mt-2 font-mono text-xs text-[var(--color-fg-muted)]">Reference: {api.requestId}</p>
      ) : null}
      {canRetry ? (
        <Button className="mt-3" onClick={onRetry} disabled={api?.status === 429 && !!left}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

/** Graph/timeline truncation: persistent, never dismissible (plan §7). */
export function TruncationBanner({ children }: { children: ReactNode }) {
  return (
    <div role="status" className="rounded-md border border-[var(--color-warn)] bg-[var(--color-surface)] px-3 py-2 text-sm">
      <span aria-hidden="true">◐ </span>
      <strong>Partial view.</strong> {children}
    </div>
  );
}

/** Shown on pages whose backend endpoint has no instant parameter (plan §8). */
export function NotTimeFiltered() {
  return (
    <span className="inline-flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-0.5 text-xs text-[var(--color-fg-muted)]">
      <span aria-hidden="true">⏱</span> Not time-filtered
    </span>
  );
}

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <h1 className="reading text-2xl font-semibold tracking-tight">{title}</h1>
      <div className="flex flex-wrap items-center gap-2">{children}</div>
    </div>
  );
}
