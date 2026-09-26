import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "./client";

/**
 * Retry policy (docs/PHASE10_PLAN.md §18): never retry 4xx (a 401 is handled
 * globally; 403/404/409/422 are answers, not glitches); retry a 429 ONCE after
 * the server's Retry-After; retry network/5xx at most twice with backoff.
 * Mutations never retry automatically.
 */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (!(error instanceof ApiError)) return failureCount < 2;
  if (error.status === 429) return failureCount < 1;
  if (error.status >= 400 && error.status < 500) return false;
  return failureCount < 2;
}

export function retryDelay(attempt: number, error: unknown): number {
  if (error instanceof ApiError && error.status === 429 && error.retryAfter !== null) {
    return Math.min(error.retryAfter, 120) * 1000;
  }
  return Math.min(1000 * 2 ** attempt, 8000);
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        retryDelay,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
}
