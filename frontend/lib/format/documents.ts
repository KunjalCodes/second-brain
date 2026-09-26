/** Document error codes (backend app/documents/*) in plain language. */
export const DOCUMENT_ERROR: Record<string, string> = {
  empty_document: "The file is empty.",
  file_too_large: "The file is larger than the server allows (20 MB by default).",
  unsupported_mime: "Unsupported format. Use plain text, Markdown, or a PDF with selectable text.",
  malformed_pdf: "The PDF couldn't be read (it may be damaged or encrypted).",
  no_extractable_text: "No selectable text was found. Scanned or image-only PDFs aren't supported (no OCR).",
  too_many_pages: "The PDF has more pages than the server allows (200 by default).",
  too_many_chunks: "The document is too long to process (chunk limit exceeded).",
  parse_timeout: "Reading the document took too long and was stopped.",
  storage_unavailable: "The stored file wasn't available for processing.",
  document_missing: "The stored file was missing when processing started.",
  internal_error: "An internal error stopped processing.",
};

export function documentErrorText(code: string | null | undefined, detail?: string | null): string {
  if (!code) return detail ?? "Processing failed.";
  return DOCUMENT_ERROR[code] ?? detail ?? `Processing failed (${code}).`;
}

/** Client-side PRE-CHECK only (UX); the server validates authoritatively. */
export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
export const ACCEPT = ".txt,.md,.markdown,.pdf,text/plain,text/markdown,application/pdf";

/** Bounded polling (plan §11): 3 s for a minute, then 15 s, stop after 10 minutes. */
export function pollInterval(elapsedMs: number): number | false {
  if (elapsedMs > 10 * 60_000) return false;
  return elapsedMs < 60_000 ? 3_000 : 15_000;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
