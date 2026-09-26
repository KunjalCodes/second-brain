import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiRequest, buildUrl, setTransport, setUnauthorizedHandler } from "@/lib/api/client";
import { retryDelay, shouldRetry } from "@/lib/api/queryClient";
import { fakeTransport, json } from "./fixtures";

afterEach(() => {
  setTransport(null);
  setUnauthorizedHandler(null);
});

describe("buildUrl", () => {
  it("prefixes the same-origin proxy and repeats array params", () => {
    expect(buildUrl("/memories/search", { status: ["active", "historical"], q: "", entity: null, limit: 25 })).toBe(
      "/api/memories/search?status=active&status=historical&limit=25",
    );
  });
});

describe("apiRequest", () => {
  it("always sends credentials and disables caching; never an Authorization header", async () => {
    const { transport, calls } = fakeTransport(() => json(200, { ok: true }));
    setTransport(transport);
    await apiRequest("/auth/me");
    const init = calls[0]!.init;
    expect(init.credentials).toBe("include");
    expect(init.cache).toBe("no-store");
    expect(Object.keys(init.headers as Record<string, string>).map((h) => h.toLowerCase())).not.toContain("authorization");
  });

  it("maps the backend error envelope, request id and Retry-After", async () => {
    setTransport(fakeTransport(() =>
      json(429, { error: { code: "rate_limited", message: "Slow down", details: { limit_class: "search" } } }, { "x-request-id": "req-1", "retry-after": "17" }),
    ).transport);
    const err = (await apiRequest("/search").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code, err.message, err.requestId, err.retryAfter]).toEqual([429, "rate_limited", "Slow down", "req-1", 17]);
  });

  it("maps FastAPI's plain `detail` shape", async () => {
    setTransport(fakeTransport(() => json(404, { detail: "Not Found" })).transport);
    const err = (await apiRequest("/nope").catch((e) => e)) as ApiError;
    expect([err.status, err.code, err.message]).toEqual([404, "http_404", "Not Found"]);
  });

  it("turns a transport failure into a network ApiError (status 0)", async () => {
    setTransport(async () => {
      throw new TypeError("fetch failed");
    });
    const err = (await apiRequest("/x").catch((e) => e)) as ApiError;
    expect(err.isNetwork).toBe(true);
    expect(err.code).toBe("network_error");
  });

  it("calls the global 401 handler on a lost session", async () => {
    const onUnauth = vi.fn();
    setUnauthorizedHandler(onUnauth);
    setTransport(fakeTransport(() => json(401, { error: { code: "authentication_required", message: "x" } })).transport);
    await apiRequest("/memories/search").catch(() => undefined);
    expect(onUnauth).toHaveBeenCalledOnce();
  });

  it("does NOT sign out on a wrong password (invalid_credentials)", async () => {
    const onUnauth = vi.fn();
    setUnauthorizedHandler(onUnauth);
    setTransport(fakeTransport(() => json(401, { error: { code: "invalid_credentials", message: "x" } })).transport);
    await apiRequest("/auth/password", { method: "POST", json: {} }).catch(() => undefined);
    expect(onUnauth).not.toHaveBeenCalled();
  });

  it("returns undefined on 204", async () => {
    setTransport(fakeTransport(() => new Response(null, { status: 204 })).transport);
    await expect(apiRequest("/auth/logout", { method: "POST" })).resolves.toBeUndefined();
  });
});

describe("retry policy", () => {
  const e = (status: number, retryAfter: number | null = null) => new ApiError(status, "c", "m", undefined, null, retryAfter);
  it.each([401, 403, 404, 409, 422])("never retries %i", (s) => expect(shouldRetry(0, e(s))).toBe(false));
  it("retries 429 once, honouring Retry-After (capped)", () => {
    expect(shouldRetry(0, e(429))).toBe(true);
    expect(shouldRetry(1, e(429))).toBe(false);
    expect(retryDelay(0, e(429, 5))).toBe(5000);
    expect(retryDelay(0, e(429, 9999))).toBe(120_000);
  });
  it("retries network / 5xx at most twice", () => {
    expect(shouldRetry(1, e(0))).toBe(true);
    expect(shouldRetry(2, e(503))).toBe(false);
  });
});
