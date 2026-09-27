"use client";
/**
 * Last-resort boundary for errors in the root layout (Phase 11, M4). It
 * replaces the whole document, so it carries its own minimal inline styling
 * (the global stylesheet is not applied here).
 */
export default function GlobalError({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "system-ui, sans-serif", margin: 0, padding: "2rem", background: "Canvas", color: "CanvasText" }}>
        <div role="alert" style={{ maxWidth: "32rem" }}>
          <title>Something went wrong</title>
          <h1 style={{ fontSize: "1.125rem" }}>Memory Explorer could not load</h1>
          <p>Something went wrong while starting the app. Your data is not affected.</p>
          {error.digest ? <p style={{ fontFamily: "monospace", fontSize: "0.75rem" }}>Reference: {error.digest}</p> : null}
          <button type="button" onClick={() => retry()} style={{ padding: "0.375rem 0.75rem" }}>
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}
