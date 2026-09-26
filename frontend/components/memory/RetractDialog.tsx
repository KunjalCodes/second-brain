"use client";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { invalidateMemory } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queryKeys";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { describeError } from "@/components/ui/States";

/**
 * Retract (backend: invalidate) a memory. Non-destructive: the memory is kept
 * but excluded from answers. The UI only updates after the server confirms;
 * legality of the transition is the server's decision (409 is shown as-is).
 */
export function RetractDialog({ memoryId }: { memoryId: string }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => invalidateMemory(memoryId, reason.trim() || null),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.memory(memoryId) }),
        queryClient.invalidateQueries({ queryKey: qk.memoriesAll }),
        queryClient.invalidateQueries({ queryKey: qk.timeline(memoryId) }),
      ]);
      setOpen(false);
    },
  });
  const err = mutation.isError ? describeError(mutation.error) : null;
  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)}>
        Retract this memory…
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Retract this memory?">
        <p className="text-sm">
          Retracted memories are <strong>kept for the record</strong> but marked as not true and excluded from answers. This does not delete anything.
        </p>
        <label className="mt-3 block text-sm">
          Reason (optional)
          <textarea
            value={reason}
            maxLength={1000}
            onChange={(e) => setReason(e.target.value)}
            className="mt-1 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1"
          />
        </label>
        {err ? (
          <p role="alert" className="mt-2 text-sm text-[var(--color-danger)]">
            {err.title}: {err.body}
          </p>
        ) : null}
        <div className="mt-4 flex justify-end gap-2">
          <Button onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant="danger" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Retracting…" : "Retract"}
          </Button>
        </div>
      </Dialog>
    </>
  );
}
