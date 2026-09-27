"use client";
/**
 * Route-level error boundary (Phase 11, M4). A rendering bug in one page shows
 * a recoverable message instead of a blank screen. Never renders the error's
 * message -- in production it may be a server digest; only the digest (a
 * reference into server logs) is shown.
 */
import Link from "next/link";
import { Button } from "@/components/ui/Button";

export default function RouteError({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <div role="alert" className="mx-auto max-w-lg space-y-3 p-6">
      <h1 className="text-lg font-semibold">This page failed to display</h1>
      <p className="text-sm text-[var(--color-fg-muted)]">
        Something went wrong while showing this page. Your data is not affected. Try again, or go back to the overview.
      </p>
      {error.digest ? <p className="font-mono text-xs text-[var(--color-fg-muted)]">Reference: {error.digest}</p> : null}
      <div className="flex gap-2">
        <Button variant="primary" onClick={() => retry()}>Try again</Button>
        <Link className="inline-flex items-center rounded-md px-3 py-1.5 text-sm underline" href="/">Overview</Link>
      </div>
    </div>
  );
}
