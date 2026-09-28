import type { Metadata } from "next";
import { connection } from "next/server";
import type { ReactNode } from "react";
import { Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Memory Explorer — AI Second Brain", template: "%s · Memory Explorer" },
  description: "Inspect, understand and control what your AI remembers.",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Render per request: the CSP script nonce (proxy.ts) is applied during
  // rendering, and a statically prerendered page could not carry it.
  await connection();
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
