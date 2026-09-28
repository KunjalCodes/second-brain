import type { NextConfig } from "next";

/**
 * Same-origin proxy (ADR 0022 / 0025): the browser only ever talks to this
 * app's own origin. `/api/*` is rewritten server-side to the FastAPI backend,
 * so the HttpOnly session cookie is first-party and no backend URL is baked
 * into the client bundle. BACKEND_ORIGIN is a SERVER-ONLY variable.
 */
const isProductionBuild = process.env.NODE_ENV === "production";

/**
 * Phase 11 (M4): a production build/start must be told where the backend is.
 * Silently falling back to localhost would ship a proxy to nowhere, and the
 * destination is baked into the build (ADR 0025). Non-HTTPS targets are
 * refused except for local production-mode runs (localhost/127.0.0.1).
 */
function resolveBackendOrigin(): string {
  const raw = process.env.BACKEND_ORIGIN?.trim();
  if (!raw) {
    if (isProductionBuild) {
      throw new Error("BACKEND_ORIGIN must be set for a production build (e.g. https://api.example.com).");
    }
    return "http://localhost:8000";
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`BACKEND_ORIGIN is not a valid URL: ${raw}`);
  }
  const local = url.hostname === "localhost" || url.hostname === "127.0.0.1";
  if (isProductionBuild && url.protocol !== "https:" && !local) {
    throw new Error(`BACKEND_ORIGIN must use https:// for a production build, got ${url.origin}`);
  }
  return url.origin;
}

const backendOrigin = resolveBackendOrigin();

/**
 * Baseline security headers (ADR 0029). The Content-Security-Policy is NOT
 * set here: it needs a fresh script nonce per request, so `proxy.ts` sets it
 * (Phase 11 M12; strict `script-src`, no 'unsafe-inline').
 */

/**
 * Phase 11 (M12 upload-path finding): the Next.js proxy BUFFERS proxied
 * request bodies and silently TRUNCATES them at 10 MB by default, so a 20 MiB
 * upload reached the backend cut short and failed after 30 s (measured). The
 * buffer limit must equal the backend's upload limit (MAX_DOCUMENT_SIZE_BYTES,
 * default 20 MiB) plus multipart framing -- the same value as
 * `backend/app/api/body_limit.py` -- so a too-large upload is refused by the
 * backend's declared-length check (413) instead of being truncated.
 * Cost: up to this many bytes are held in memory per in-flight upload.
 */
const MULTIPART_OVERHEAD_BYTES = 64 * 1024;
const maxUploadBytes = Number(process.env.MAX_DOCUMENT_SIZE_BYTES ?? 20 * 1024 * 1024) + MULTIPART_OVERHEAD_BYTES;

const nextConfig: NextConfig = {
  experimental: {
    proxyClientMaxBodySize: maxUploadBytes,
  },
  reactStrictMode: true,
  poweredByHeader: false,
  // Phase 11 (M10): opt-in source maps for bundle attribution
  // (scripts/bundle-attribution.mjs). Never on in a normal build.
  productionBrowserSourceMaps: process.env.NEXT_SOURCEMAPS === "1",
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backendOrigin}/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Referrer-Policy", value: "same-origin" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          // Phase 11 (S9). HSTS: browsers honour it only over HTTPS, so it is
          // harmless on a local `next start` and binding on Vercel.
          ...(isProductionBuild
            ? [{ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" }]
            : []),
          {
            key: "Permissions-Policy",
            value:
              "camera=(), microphone=(), geolocation=(), payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=(), browsing-topics=()",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
