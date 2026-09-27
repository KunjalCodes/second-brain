import Link from "next/link";

export default function NotFound() {
  return (
    <main id="main" className="mx-auto max-w-lg space-y-3 p-6">
      <h1 className="text-lg font-semibold">Page not found</h1>
      <p className="text-sm text-[var(--color-fg-muted)]">There is no page at this address.</p>
      <Link className="text-sm underline" href="/">Go to the overview</Link>
    </main>
  );
}
