/**
 * The ONLY place the frontend performs HTTP (docs/PHASE10_PLAN.md §18,
 * ADR 0025). Every request:
 *   - goes to this app's own origin under /api (the Next.js rewrite proxy);
 *   - carries the HttpOnly session cookie via `credentials: "include"` --
 *     JavaScript never sees or stores a token (ADR 0026);
 *   - is never cached (`cache: "no-store"`; the backend also sends
 *     `Cache-Control: no-store`, Phase 10 G2).
 * Non-2xx responses become a typed `ApiError`; domain states that are not
 * errors (truncation flags, evidence status, provenance nulls, …) are passed
 * through untouched -- the client never "fixes up" backend data.
 */

export const API_BASE = "/api";

export type QueryValue = string | number | boolean | null | undefined;
export type Query = Record<string, QueryValue | QueryValue[]>;

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  query?: Query;
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal;
  /** Milliseconds; defaults per request kind (see TIMEOUTS). */
  timeoutMs?: number;
}

export const TIMEOUTS = { default: 30_000, agent: 90_000, upload: 120_000 } as const;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: unknown = undefined,
    readonly requestId: string | null = null,
    /** Seconds, from the `Retry-After` header on 429. */
    readonly retryAfter: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
  get isNetwork(): boolean {
    return this.status === 0;
  }
}

export type Transport = (url: string, init: RequestInit) => Promise<Response>;

let transport: Transport = (url, init) => fetch(url, init);
let onUnauthorized: (() => void) | null = null;

/** Tests inject a fake transport; production uses `fetch`. */
export function setTransport(t: Transport | null): void {
  transport = t ?? ((url, init) => fetch(url, init));
}

/** The auth layer registers the global 401 policy (redirect to login). */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

export function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, raw] of Object.entries(query ?? {})) {
    const values = Array.isArray(raw) ? raw : [raw];
    for (const v of values) {
      if (v === undefined || v === null || v === "") continue;
      params.append(key, String(v));
    }
  }
  const qs = params.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

async function toApiError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("x-request-id");
  const retryHeader = response.headers.get("retry-after");
  const retryAfter = retryHeader !== null && /^\d+$/.test(retryHeader) ? Number(retryHeader) : null;
  let code = `http_${response.status}`;
  let message = response.statusText || "Request failed";
  let details: unknown;
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string; details?: unknown; request_id?: string };
      detail?: unknown;
    };
    if (body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      details = body.error.details;
    } else if (body.detail !== undefined) {
      // FastAPI's own shape (e.g. the unknown-route 404).
      message = typeof body.detail === "string" ? body.detail : message;
      details = body.detail;
    }
  } catch {
    /* non-JSON error body: keep the status text */
  }
  return new ApiError(response.status, code, message, details, requestId, retryAfter);
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", query, json, form, signal, timeoutMs = TIMEOUTS.default } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new DOMException("timeout", "TimeoutError")), timeoutMs);
  const abortFromCaller = () => controller.abort(signal?.reason);
  signal?.addEventListener("abort", abortFromCaller, { once: true });

  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form; // the browser sets the multipart boundary
  }

  let response: Response;
  try {
    response = await transport(buildUrl(path, query), {
      method,
      headers,
      body,
      credentials: "include",
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (err) {
    if (signal?.aborted) throw err; // caller cancelled (navigation): not an error to show
    const timedOut = controller.signal.aborted;
    throw new ApiError(
      0,
      timedOut ? "timeout" : "network_error",
      timedOut ? "The server took too long to respond." : "Could not reach the server.",
    );
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abortFromCaller);
  }

  if (!response.ok) {
    const error = await toApiError(response);
    // `invalid_credentials` means a wrong password on login / password change;
    // the session (if any) is still valid, so it is not a sign-out event.
    if (error.status === 401 && error.code !== "invalid_credentials" && onUnauthorized) onUnauthorized();
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
