"use client";
/**
 * Session state for the browser (ADR 0026). The HttpOnly cookie is the only
 * credential; `GET /auth/me` is the authoritative check. Any 401 from any
 * request clears all cached (per-user) data and returns to the login page.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useMemo, useRef, type ReactNode } from "react";
import { ApiError, setUnauthorizedHandler } from "@/lib/api/client";
import { getMe, logout as apiLogout } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import type { MeResponse } from "@/lib/api/types";

interface AuthState {
  me: MeResponse | undefined;
  status: "loading" | "authenticated" | "unauthenticated" | "error";
  error: unknown;
  refetch: () => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function loginPath(current: string, reason?: "expired"): string {
  const params = new URLSearchParams();
  if (current && current !== "/") params.set("next", current);
  if (reason) params.set("reason", reason);
  const qs = params.toString();
  return `/login${qs ? `?${qs}` : ""}`;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const hadSession = useRef(false);
  const redirecting = useRef(false);

  const query = useQuery({ queryKey: qk.me, queryFn: ({ signal }) => getMe(signal), retry: false, staleTime: 60_000 });

  useEffect(() => {
    if (query.data) hadSession.current = true;
  }, [query.data]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (redirecting.current) return;
      redirecting.current = true;
      const expired = hadSession.current;
      queryClient.clear();
      const current = window.location.pathname + window.location.search;
      router.replace(loginPath(current, expired ? "expired" : undefined));
    });
    return () => setUnauthorizedHandler(null);
  }, [queryClient, router]);

  useEffect(() => {
    redirecting.current = false;
  }, [pathname]);

  const value = useMemo<AuthState>(() => {
    const unauth = query.error instanceof ApiError && query.error.status === 401;
    return {
      me: query.data,
      status: query.isPending ? "loading" : query.data ? "authenticated" : unauth ? "unauthenticated" : "error",
      error: query.error,
      refetch: () => void query.refetch(),
      signOut: async () => {
        try {
          await apiLogout();
        } finally {
          hadSession.current = false;
          queryClient.clear();
          router.replace("/login");
        }
      },
    };
  }, [query, queryClient, router]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
