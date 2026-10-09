"use client";
import Link from "next/link";
import { useState } from "react";
import { ApiError } from "@/lib/api/client";
import { register } from "@/lib/api/endpoints";
import { REGISTRATION_ENABLED } from "@/lib/auth/safeNext";
import { Button } from "@/components/ui/Button";

export function RegisterForm() {
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<string | null>(null);

  if (!REGISTRATION_ENABLED) {
    return (
      <div className="max-w-sm panel p-6 text-sm">
        <h1 className="mb-2 text-lg font-semibold">Registration is closed</h1>
        <p>Accounts on this deployment are created by the operator. Ask them for access.</p>
        <p className="mt-3">
          <Link className="underline" href="/login">Back to sign in</Link>
        </p>
      </div>
    );
  }

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setState("busy");
    setError(null);
    try {
      await register(String(form.get("email") ?? ""), String(form.get("password") ?? ""));
      setState("done");
    } catch (err) {
      setState("idle");
      if (err instanceof ApiError && err.status === 404) setError("Registration is closed on this server. Ask the operator for an account.");
      else if (err instanceof ApiError && err.status === 409) setError("An account with this email already exists.");
      else if (err instanceof ApiError && err.status === 422) setError("Use a valid email and a password of 12–128 characters that isn't your email.");
      else if (err instanceof ApiError && err.status === 429) setError(`Too many attempts. Try again in ${err.retryAfter ?? "a few"} seconds.`);
      else setError("Registration failed. Try again.");
    }
  }

  if (state === "done") {
    return (
      <div className="max-w-sm panel p-6 text-sm">
        <h1 className="mb-2 text-lg font-semibold">Account created</h1>
        <p>You can now sign in.</p>
        <p className="mt-3">
          <Link className="underline" href="/login">Sign in</Link>
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="w-full max-w-sm space-y-4 panel p-6">
      <h1 className="text-lg font-semibold">Create an account</h1>
      <label className="block text-sm">
        Email
        <input name="email" type="email" autoComplete="username" required className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2" />
      </label>
      <label className="block text-sm">
        Password <span className="text-[var(--color-fg-muted)]">(12–128 characters)</span>
        <input name="password" type="password" autoComplete="new-password" required minLength={12} maxLength={128} className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2" />
      </label>
      {error ? <p role="alert" className="text-sm text-[var(--color-danger)]">{error}</p> : null}
      <Button type="submit" variant="primary" disabled={state === "busy"} className="w-full justify-center">
        {state === "busy" ? "Creating…" : "Create account"}
      </Button>
    </form>
  );
}
