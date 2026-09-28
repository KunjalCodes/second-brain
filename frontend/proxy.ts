import { NextResponse, type NextRequest } from "next/server";

/**
 * Per-request Content-Security-Policy with a script nonce (Phase 11 M12).
 *
 * `script-src` allows only scripts carrying this request's nonce (Next.js
 * attaches it to its own bootstrap, runtime and page scripts during dynamic
 * rendering) and, through 'strict-dynamic', the chunks those scripts load.
 * No 'unsafe-inline': an injected <script> without the nonce does not run.
 * See node_modules/next/dist/docs/01-app/02-guides/content-security-policy.md.
 *
 * `style-src` keeps 'unsafe-inline' on purpose: React renders `style`
 * attributes (layout widths, the graph container), which a nonce cannot
 * cover, and style injection cannot execute code. No backend content is ever
 * rendered as HTML (ADR 0029), so this is defence in depth, not the only
 * defence.
 *
 * Nonces require every page to be rendered per request; the root layout
 * opts in with `connection()`.
 */
export function buildCsp(nonce: string, isDev: boolean): string {
  return [
    "default-src 'self'",
    // 'unsafe-eval' only in development (React dev tooling).
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
}

export function proxy(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());
  const csp = buildCsp(nonce, process.env.NODE_ENV === "development");

  // Next.js reads the nonce from the request's CSP header while rendering.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      // Documents only: not the backend proxy (/api), static assets, or
      // router prefetches (which carry no document).
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
