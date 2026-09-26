import type { NextConfig } from "next";

/**
 * Same-origin proxy (ADR 0022 / 0025): the browser only ever talks to this
 * app's own origin. `/api/*` is rewritten server-side to the FastAPI backend,
 * so the HttpOnly session cookie is first-party and no backend URL is baked
 * into the client bundle. BACKEND_ORIGIN is a SERVER-ONLY variable.
 */
const backendOrigin = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";

/**
 * Baseline security headers (ADR 0029). The CSP forbids plugins, framing and
 * foreign connections; the API is same-origin so `connect-src 'self'`.
 * Next.js injects small inline bootstrap scripts, hence 'unsafe-inline' for
 * script-src in production builds; 'unsafe-eval' only in development (React
 * dev tooling). No backend content is ever rendered as HTML, so the XSS
 * defence does not rest on the CSP alone.
 */
const isDev = process.env.NODE_ENV !== "production";
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backendOrigin}/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: csp },
          { key: "Referrer-Policy", value: "same-origin" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};

export default nextConfig;
