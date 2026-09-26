"use client";
import type { ReactNode } from "react";
import { AuthProvider } from "@/lib/auth/AuthProvider";
import { TimeLensProvider } from "@/lib/time/lens";
import { AppShell } from "@/components/layout/AppShell";

export default function AuthenticatedLayout({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <TimeLensProvider>
        <AppShell>{children}</AppShell>
      </TimeLensProvider>
    </AuthProvider>
  );
}
