"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/lib/api/client";
import { login } from "@/lib/api/endpoints";
import { REGISTRATION_ENABLED, safeNext } from "@/lib/auth/safeNext";
import { Button } from "@/components/ui/Button";

export function loginErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong. Try again.";
  if (error.isNetwork) return "Can't reach the server. It may be starting up — try again in a moment.";
  if (error.status === 401) return "Email or password is incorrect.";
  if (error.status === 403 && error.code === "account_disabled") return "This account is disabled.";
  if (error.status === 403 && error.code === "origin_not_allowed")
    return `This deployment is misconfigured (origin not allowed). Reference: ${error.requestId ?? "n/a"}`;
  if (error.status === 429)
    return `Too many attempts. Try again in ${error.retryAfter ?? "a few"} seconds.`;
  if (error.status === 422) return "Enter a valid email and password.";
  return "Sign-in failed. Try again.";
}

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const expired = params.get("reason") === "expired";
  const next = safeNext(params.get("next"));

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      // The session token arrives ONLY as an HttpOnly cookie; the response body
      // carries no token because `token_in_body` is never requested here.
      await login(String(form.get("email") ?? ""), String(form.get("password") ?? ""));
      queryClient.clear();
      router.replace(next);
    } catch (err) {
      setError(loginErrorMessage(err));
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={onSubmit}
      aria-labelledby="login-title"
      className="w-full max-w-sm space-y-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-6"
    >
      <div>
        <h1 id="login-title" className="text-lg font-semibold">
          Sign in to Memory Explorer
        </h1>
        <p className="text-sm text-[var(--color-fg-muted)]">Inspect what your AI remembers.</p>
      </div>
      {expired ? (
        <p role="status" className="rounded border border-[var(--color-warn)] px-3 py-2 text-sm">
          Your session ended. Sign in again.
        </p>
      ) : null}
      <label className="block text-sm">
        Email
        <input
          name="email"
          type="email"
          autoComplete="username"
          required
          className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2"
        />
      </label>
      <label className="block text-sm">
        Password
        <input
          name="password"
          type="password"
          autoComplete="current-password"
          required
          className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2"
        />
      </label>
      {error ? (
        <p role="alert" className="text-sm text-[var(--color-danger)]">
          {error}
        </p>
      ) : null}
      <Button type="submit" variant="primary" disabled={busy} className="w-full justify-center">
        {busy ? "Signing in…" : "Sign in"}
      </Button>
      {REGISTRATION_ENABLED ? (
        <p className="text-center text-sm">
          No account? <Link className="underline" href="/register">Create one</Link>
        </p>
      ) : null}
    </form>
  );
}
