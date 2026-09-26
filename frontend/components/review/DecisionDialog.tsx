"use client";
/**
 * Confirms a review decision with its consequence stated first. The UI only
 * reflects a decision after the server confirms it; on 409 (stale
 * `if_version`) it asks the user to reload rather than retrying.
 */
import { useState } from "react";
import { ApiError } from "@/lib/api/client";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { describeError } from "@/components/ui/States";

export function DecisionDialog({
  open,
  title,
  consequence,
  confirmLabel,
  pending,
  error,
  onConfirm,
  onClose,
  onReload,
}: {
  open: boolean;
  title: string;
  consequence: React.ReactNode;
  confirmLabel: string;
  pending: boolean;
  error: unknown;
  onConfirm: (reason: string | null) => void;
  onClose: () => void;
  onReload: () => void;
}) {
  const [reason, setReason] = useState("");
  const stale = error instanceof ApiError && error.status === 409;
  const err = error ? describeError(error) : null;
  return (
    <Dialog open={open} onClose={onClose} title={title}>
      <div className="text-sm">{consequence}</div>
      <label className="mt-3 block text-sm">
        Reason (optional, kept in the decision history)
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1" />
      </label>
      {stale ? (
        <p role="alert" className="mt-2 text-sm text-[var(--color-warn)]">
          This item changed since you opened it. <Button onClick={onReload}>Reload</Button>
        </p>
      ) : err ? (
        <p role="alert" className="mt-2 text-sm text-[var(--color-danger)]">{err.title}: {err.body}</p>
      ) : null}
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={pending || stale} onClick={() => onConfirm(reason.trim() || null)}>
          {pending ? "Saving…" : confirmLabel}
        </Button>
      </div>
    </Dialog>
  );
}
