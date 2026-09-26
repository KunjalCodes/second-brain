import { Button } from "./Button";

export function Pagination({ page, pageSize, total, onPage }: { page: number; pageSize: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return null;
  return (
    <nav aria-label="Pagination" className="flex items-center gap-2 pt-3 text-sm">
      <Button onClick={() => onPage(page - 1)} disabled={page <= 1} aria-label="Previous page">
        ← Prev
      </Button>
      <span aria-live="polite">
        Page {page} of {pages} · {total} total
      </span>
      <Button onClick={() => onPage(page + 1)} disabled={page >= pages} aria-label="Next page">
        Next →
      </Button>
    </nav>
  );
}
