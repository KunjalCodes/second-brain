"use client";
/**
 * A modal built on the native <dialog> element: showModal() gives focus
 * trapping, Esc-to-close and an inert background with no dependency.
 */
import { useEffect, useRef, type ReactNode } from "react";

export function Dialog({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal?.();
    if (!open && el.open) el.close?.();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-labelledby="dialog-title"
      className="m-auto w-[min(560px,92vw)] rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-0 text-[var(--color-fg)]"
    >
      {open ? (
        <div className="p-5">
          <h2 id="dialog-title" className="mb-3 text-base font-semibold">
            {title}
          </h2>
          {children}
        </div>
      ) : null}
    </dialog>
  );
}
