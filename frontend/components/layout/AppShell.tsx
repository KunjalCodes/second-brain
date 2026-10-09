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

interface NavItem {
  href: string;
  label: string;
  glyph: string;
}
/** Knowledge and exploration first, not database tables (docs/OBSIDIAN_UI_PLAN.md §3). */
const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: "Workspace",
    items: [
      { href: "/", label: "Overview", glyph: "◈" },
      { href: "/memories", label: "Memories", glyph: "❝" },
      { href: "/entities", label: "Entities", glyph: "◇" },
      { href: "/graph", label: "Graph", glyph: "✳" },
      { href: "/projects", label: "Projects", glyph: "◆" },
      { href: "/documents", label: "Documents", glyph: "▤" },
    ],
  },
  { title: "Explore", items: [{ href: "/search", label: "Search", glyph: "⌕" }] },
  {
    title: "Intelligence",
    items: [
      { href: "/ask", label: "Ask", glyph: "✦" },
      { href: "/review", label: "Review", glyph: "⚖" },
    ],
  },
];
const NAV = NAV_GROUPS.flatMap((g) => g.items);

const COLLAPSE_KEY = "nav-collapsed";

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

/** A pure UI preference, held per browser; the shell renders correctly without storage. */
function useNavCollapsed(): [boolean, (v: boolean) => void] {
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    try {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setCollapsed(window.localStorage.getItem(COLLAPSE_KEY) === "1");
    } catch {
      /* storage blocked: stay expanded */
    }
  }, []);
  const set = (v: boolean) => {
    setCollapsed(v);
    try {
      window.localStorage.setItem(COLLAPSE_KEY, v ? "1" : "0");
    } catch {
      /* ignore */
    }
  };
  return [collapsed, set];
}

function ReviewBadge() {
  const mem = useQuery({ queryKey: qk.reviewMemories(1), queryFn: ({ signal }) => listMemoryReview(25, 0, signal) });
  const ent = useQuery({ queryKey: qk.reviewEntities(1), queryFn: ({ signal }) => listEntityReview(25, 0, signal) });
  const total = (mem.data?.total ?? 0) + (ent.data?.total ?? 0);
  if (!total) return null;
  return (
    <span className="ml-auto rounded-full bg-[var(--color-warn)] px-1.5 text-xs font-semibold text-[var(--color-bg)]" aria-label={`${total} pending`}>
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
  const [collapsed, setCollapsed] = useNavCollapsed();
  // Phase 17: keyed by the selected memory space so page-local state (e.g. a loaded graph)
  // cannot survive a switch and show one space's data under another's name.
  const space = useSyncExternalStore(subscribeMemorySpace, getMemorySpace, () => null);
  const current = NAV.find((i) => isActive(pathname, i.href));

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
          className={`${navOpen ? "flex" : "hidden"} glass fixed inset-y-2 left-2 z-40 w-56 flex-col p-3 lg:sticky lg:top-2 lg:my-2 lg:ml-2 lg:flex lg:h-[calc(100vh-1rem)] ${
            collapsed ? "lg:w-16" : "lg:w-56"
          } transition-[width] duration-150`}
        >
          <div className="mb-3 flex items-center justify-between px-1 text-sm font-semibold">
            <span className="flex items-center gap-2">
              <span aria-hidden="true" className="text-lg text-[var(--color-accent)]">◈</span>
              <span className={collapsed ? "lg:sr-only" : ""}>Second Brain</span>
            </span>
            <button
              type="button"
              onClick={() => setCollapsed(!collapsed)}
              aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
              aria-expanded={!collapsed}
              className="hidden rounded-md px-1.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-surface-2)] lg:block"
            >
              <span aria-hidden="true">{collapsed ? "»" : "«"}</span>
            </button>
          </div>
          <div className="flex-1 space-y-4 overflow-y-auto">
            {NAV_GROUPS.map((group) => (
              <div key={group.title}>
                <p className={`mb-1 px-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--color-fg-muted)] ${collapsed ? "lg:sr-only" : ""}`}>
                  {group.title}
                </p>
                <ul className="space-y-0.5">
                  {group.items.map((item) => {
                    const active = isActive(pathname, item.href);
                    return (
                      <li key={item.href}>
                        <Link
                          href={item.href}
                          aria-current={active ? "page" : undefined}
                          title={collapsed ? item.label : undefined}
                          className={`flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm transition-colors ${
                            active
                              ? "bg-[var(--color-surface-2)] font-semibold text-[var(--color-accent)]"
                              : "hover:bg-[var(--color-surface-2)]"
                          }`}
                        >
                          <span aria-hidden="true" className="w-5 text-center">{item.glyph}</span>
                          <span className={collapsed ? "lg:sr-only" : ""}>{item.label}</span>
                          {item.href === "/review" ? <ReviewBadge /> : null}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </div>
          <div className="mt-2 space-y-1 border-t border-[var(--color-border)] pt-3 text-sm">
            <p className={`truncate px-2 text-[var(--color-fg-muted)] ${collapsed ? "lg:sr-only" : ""}`} title={auth.me?.user.email ?? undefined}>
              {auth.me?.user.email ?? auth.me?.user.id}
            </p>
            <Link href="/settings" className="block rounded-lg px-2 py-1.5 hover:bg-[var(--color-surface-2)]">
              <span aria-hidden="true" className="inline-block w-5 text-center">⚙</span>
              <span className={collapsed ? "lg:sr-only" : ""}> Account & sessions</span>
            </Link>
            <button type="button" onClick={() => void auth.signOut()} className="block w-full rounded-lg px-2 py-1.5 text-left hover:bg-[var(--color-surface-2)]">
              <span aria-hidden="true" className="inline-block w-5 text-center">⇥</span>
              <span className={collapsed ? "lg:sr-only" : ""}> Sign out</span>
            </button>
          </div>
        </nav>
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="glass sticky top-2 z-30 mx-2 my-2 flex flex-wrap items-center gap-3 px-4 py-2 lg:mx-3">
            <Button className="lg:hidden" aria-expanded={navOpen} aria-label="Toggle navigation" onClick={() => setNavOpen((o) => !o)}>
              ☰
            </Button>
            <p className="hidden text-sm text-[var(--color-fg-muted)] sm:block" aria-label="You are here">
              {space ? "Project memory" : "Personal memory"}
              {current ? <> <span aria-hidden="true">/</span> <span className="text-[var(--color-fg)]">{current.label}</span></> : null}
            </p>
            <div className="ml-auto flex flex-wrap items-center gap-3">
              <ProjectSwitcher />
              <TimeLensControl />
            </div>
          </header>
          <main id="main" key={space ?? "personal"} className="min-w-0 flex-1 px-3 pb-8 pt-2 lg:px-5">
            {children}
          </main>
        </div>
      </div>
    </AuthGate>
  );
}
