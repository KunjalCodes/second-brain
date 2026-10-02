"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { getMemorySpace, subscribeMemorySpace } from "@/lib/api/space";
import { useAuth } from "@/lib/auth/AuthProvider";
import { listEntityReview, listMemoryReview } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { TimeLensControl } from "./TimeLensControl";
import { ProjectSwitcher } from "./ProjectSwitcher";
import { Button } from "@/components/ui/Button";
import { ErrorState } from "@/components/ui/States";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/memories", label: "Memories" },
  { href: "/entities", label: "Entities" },
  { href: "/graph", label: "Graph" },
  { href: "/search", label: "Search" },
  { href: "/documents", label: "Documents" },
  { href: "/review", label: "Review" },
  { href: "/ask", label: "Ask" },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

function ReviewBadge() {
  const mem = useQuery({ queryKey: qk.reviewMemories(1), queryFn: ({ signal }) => listMemoryReview(25, 0, signal) });
  const ent = useQuery({ queryKey: qk.reviewEntities(1), queryFn: ({ signal }) => listEntityReview(25, 0, signal) });
  const total = (mem.data?.total ?? 0) + (ent.data?.total ?? 0);
  if (!total) return null;
  return (
    <span className="ml-auto rounded-full bg-[var(--color-warn)] px-1.5 text-xs font-semibold text-white" aria-label={`${total} pending`}>
      {total}
    </span>
  );
}

/** Renders nothing protected until `/auth/me` confirms the session (ADR 0026). */
function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (auth.status !== "loading") return;
    const t = setTimeout(() => setSlow(true), 4000);
    return () => clearTimeout(t);
  }, [auth.status]);

  if (auth.status === "loading" || auth.status === "unauthenticated") {
    return (
      <div role="status" aria-label="Checking your session" className="flex min-h-screen items-center justify-center p-6 text-sm text-[var(--color-fg-muted)]">
        {slow ? "Waking the server — this can take up to a minute on a free-tier demo…" : "Checking your session…"}
      </div>
    );
  }
  if (auth.status === "error") {
    return (
      <div className="mx-auto mt-16 max-w-md p-4">
        <ErrorState error={auth.error} onRetry={auth.refetch} />
      </div>
    );
  }
  return <>{children}</>;
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const auth = useAuth();
  const [navOpen, setNavOpen] = useState(false);
  // Phase 17: keyed by the selected memory space so page-local state (e.g. a loaded graph)
  // cannot survive a switch and show one space's data under another's name.
  const space = useSyncExternalStore(subscribeMemorySpace, getMemorySpace, () => null);

  return (
    <AuthGate>
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-[var(--color-surface)] focus:px-3 focus:py-2">
        Skip to content
      </a>
      <div className="flex min-h-screen">
        <nav
          // Close the mobile drawer when a link inside it is followed.
          onClick={(e) => {
            if ((e.target as HTMLElement).closest("a")) setNavOpen(false);
          }}
          aria-label="Main"
          className={`${navOpen ? "flex" : "hidden"} fixed inset-y-0 left-0 z-40 w-56 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)] p-3 lg:static lg:flex`}
        >
          <div className="mb-4 px-2 text-sm font-semibold">
            <span aria-hidden="true">◈ </span>Memory Explorer
          </div>
          <ul className="space-y-0.5">
            {NAV.map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={isActive(pathname, item.href) ? "page" : undefined}
                  className={`flex items-center rounded-md px-2 py-1.5 text-sm ${
                    isActive(pathname, item.href)
                      ? "bg-[var(--color-surface-2)] font-semibold"
                      : "hover:bg-[var(--color-surface-2)]"
                  }`}
                >
                  {item.label}
                  {item.href === "/review" ? <ReviewBadge /> : null}
                </Link>
              </li>
            ))}
          </ul>
          <div className="mt-auto space-y-1 border-t border-[var(--color-border)] pt-3 text-sm">
            <p className="truncate px-2 text-[var(--color-fg-muted)]" title={auth.me?.user.email ?? undefined}>
              {auth.me?.user.email ?? auth.me?.user.id}
            </p>
            <Link href="/settings" className="block rounded-md px-2 py-1.5 hover:bg-[var(--color-surface-2)]">
              Account & sessions
            </Link>
            <button type="button" onClick={() => void auth.signOut()} className="block w-full rounded-md px-2 py-1.5 text-left hover:bg-[var(--color-surface-2)]">
              Sign out
            </button>
          </div>
        </nav>
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex flex-wrap items-center gap-3 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-2">
            <Button className="lg:hidden" aria-expanded={navOpen} aria-label="Toggle navigation" onClick={() => setNavOpen((o) => !o)}>
              ☰
            </Button>
            <ProjectSwitcher />
            <TimeLensControl />
          </header>
          <main id="main" key={space ?? "personal"} className="min-w-0 flex-1 p-4 lg:p-6">
            {children}
          </main>
        </div>
      </div>
    </AuthGate>
  );
}
