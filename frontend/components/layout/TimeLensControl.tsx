"use client";
import { useTimeLens } from "@/lib/time/lens";
import { formatDateTime, isoToLocalInput, localInputToIso } from "@/lib/format/dates";

/** Global "Now / As of" control. Selects the instant; the backend decides what was true. */
export function TimeLensControl() {
  const { asOf, setAsOf } = useTimeLens();
  return (
    <fieldset className="flex items-center gap-2 text-sm">
      <legend className="sr-only">Time lens</legend>
      <label className="inline-flex items-center gap-1">
        <input type="radio" name="lens" checked={asOf === null} onChange={() => setAsOf(null)} /> Now
      </label>
      <label className="inline-flex items-center gap-1">
        <input
          type="radio"
          name="lens"
          checked={asOf !== null}
          onChange={() => setAsOf(asOf ?? new Date().toISOString())}
        />
        As of
      </label>
      <input
        type="datetime-local"
        aria-label="As-of date and time"
        disabled={asOf === null}
        value={isoToLocalInput(asOf)}
        onChange={(e) => {
          const iso = localInputToIso(e.target.value);
          if (iso) setAsOf(iso);
        }}
        className="rounded border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1 disabled:opacity-50"
      />
      {asOf ? (
        <span className="text-xs text-[var(--color-warn)]" role="status">
          Viewing as of {formatDateTime(asOf)}
        </span>
      ) : null}
    </fieldset>
  );
}
