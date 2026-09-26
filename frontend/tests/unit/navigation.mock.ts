/** Shared mocks for Next.js navigation in component tests. */
import { vi } from "vitest";

export const nav = {
  replace: vi.fn(),
  push: vi.fn(),
  params: new URLSearchParams(),
  pathname: "/",
};

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: nav.push, back: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => nav.params,
  usePathname: () => nav.pathname,
}));

vi.mock("next/link", async () => {
  const React = await import("react");
  return {
    default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
      React.createElement("a", { href, ...rest }, children),
  };
});
