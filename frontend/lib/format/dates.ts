/**
 * Two clocks, never merged (ADR 0028):
 *   valid time    -- "True from … until …"  (valid_from / valid_until)
 *   recorded time -- "Recorded …" / "Updated …" (created_at / updated_at)
 * Formatting only; no temporal arithmetic (the backend computes temporal state).
 */
const DATE = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });
const DATETIME = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZoneName: "short",
});

export function formatDate(iso: string | null | undefined): string {
  return iso ? DATE.format(new Date(iso)) : "—";
}

export function formatDateTime(iso: string | null | undefined): string {
  return iso ? DATETIME.format(new Date(iso)) : "—";
}

export function validityText(from: string | null | undefined, until: string | null | undefined): string {
  if (!from && !until) return "True with no recorded start or end";
  if (from && !until) return `True since ${formatDate(from)} (no known end)`;
  if (!from && until) return `True until ${formatDate(until)}`;
  return `True from ${formatDate(from)} until ${formatDate(until)}`;
}

/**
 * The global time lens sends a timezone-AWARE instant: the backend rejects
 * naive datetimes (422). `datetime-local` input values are local wall time;
 * convert to an ISO string with offset (UTC `Z`).
 */
export function localInputToIso(value: string): string | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function isoToLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
