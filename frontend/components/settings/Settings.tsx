"use client";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api/client";
import { changePassword, logoutAll } from "@/lib/api/endpoints";
import { useAuth } from "@/lib/auth/AuthProvider";
import { formatDateTime } from "@/lib/format/dates";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { describeError, PageHeader } from "@/components/ui/States";

const MIN = 12;
const MAX = 128;

export function passwordErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "invalid_credentials") return "Your current password is incorrect.";
    if (err.status === 422) return err.message || `Choose a password of ${MIN}-${MAX} characters that isn't your email address.`;
    if (err.status === 429) return "Too many attempts. Wait a few minutes and try again.";
  }
  const d = describeError(err);
  return `${d.title}: ${d.body}`;
}

function PasswordForm() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const m = useMutation({
    mutationFn: () => changePassword(current, next),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setRepeat("");
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setLocalError(null);
    m.reset();
    if (next.length < MIN || next.length > MAX) return setLocalError(`The new password must be ${MIN}-${MAX} characters.`);
    if (next !== repeat) return setLocalError("The new passwords don't match.");
    m.mutate();
  };
  const input = "mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1";
  return (
    <form onSubmit={submit} className="max-w-sm space-y-3" noValidate>
      <label className="block text-sm">Current password<input type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} className={input} /></label>
      <label className="block text-sm">New password ({MIN}-{MAX} characters)<input type="password" autoComplete="new-password" required value={next} onChange={(e) => setNext(e.target.value)} className={input} /></label>
      <label className="block text-sm">Repeat new password<input type="password" autoComplete="new-password" required value={repeat} onChange={(e) => setRepeat(e.target.value)} className={input} /></label>
      {localError || m.isError ? <p role="alert" className="text-sm text-[var(--color-danger)]">{localError ?? passwordErrorText(m.error)}</p> : null}
      {m.isSuccess ? <p role="status" className="text-sm text-[var(--color-current)]">Password changed. Your other sessions were signed out.</p> : null}
      <Button type="submit" variant="primary" disabled={m.isPending}>{m.isPending ? "Saving…" : "Change password"}</Button>
    </form>
  );
}

export function Settings() {
  const { me } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [confirmAll, setConfirmAll] = useState(false);
  const all = useMutation({
    mutationFn: logoutAll,
    // The server revoked every session and cleared the cookie: drop all cached
    // per-user data before leaving.
    onSuccess: () => {
      queryClient.clear();
      router.replace("/login");
    },
  });
  const session = me?.session;
  return (
    <div className="space-y-8">
      <PageHeader title="Settings" />
      <section aria-labelledby="acct-h" className="space-y-1 text-sm">
        <h2 id="acct-h" className="text-base font-semibold">Account</h2>
        <p>Email: {me?.user.email ?? "—"}</p>
        {session ? (
          <>
            <p>This session ends by {formatDateTime(session.expires_at)} at the latest.</p>
            <p>It also ends if unused until {formatDateTime(session.idle_expires_at)} (activity extends this).</p>
          </>
        ) : (
          <p>Signed in with a development identity header; there is no session to manage.</p>
        )}
      </section>
      {session ? (
        <>
          <section aria-labelledby="pw-h" className="space-y-2">
            <h2 id="pw-h" className="text-base font-semibold">Change password</h2>
            <PasswordForm />
          </section>
          <section aria-labelledby="all-h" className="space-y-2">
            <h2 id="all-h" className="text-base font-semibold">Sign out everywhere</h2>
            <p className="text-sm">Ends every session of this account, including this one.</p>
            <Button variant="danger" onClick={() => setConfirmAll(true)}>Sign out everywhere</Button>
            <Dialog open={confirmAll} onClose={() => setConfirmAll(false)} title="Sign out everywhere?">
              <p className="text-sm">All your sessions on every device will end, including this one.</p>
              {all.isError ? <p role="alert" className="mt-2 text-sm text-[var(--color-danger)]">{describeError(all.error).body}</p> : null}
              <div className="mt-4 flex justify-end gap-2">
                <Button onClick={() => setConfirmAll(false)}>Cancel</Button>
                <Button variant="danger" disabled={all.isPending} onClick={() => all.mutate()}>{all.isPending ? "Signing out…" : "Sign out everywhere"}</Button>
              </div>
            </Dialog>
          </section>
        </>
      ) : null}
    </div>
  );
}
