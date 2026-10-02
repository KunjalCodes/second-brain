/**
 * The selected MEMORY SPACE (Phase 16): either the account's own memory (null) or one
 * of its projects. It is only a request header (`X-Memory-Space: <project id>`); the
 * server checks ownership on every request and answers 404 for a project the account
 * does not own, so this value is a selection, never an authority. Persisted per browser.
 */
const KEY = "memory-space";
let current: string | null = null;
let loaded = false;
const listeners = new Set<() => void>();

function load(): void {
  if (loaded) return;
  loaded = true;
  try {
    const v = typeof window !== "undefined" ? window.localStorage.getItem(KEY) : null;
    current = v && /^[0-9a-fA-F-]{36}$/.test(v) ? v : null;
  } catch {
    current = null; // storage blocked: fall back to the personal space
  }
}

export function getMemorySpace(): string | null {
  load();
  return current;
}

export function setMemorySpace(projectId: string | null): void {
  load();
  current = projectId && /^[0-9a-fA-F-]{36}$/.test(projectId) ? projectId : null;
  try {
    if (current) window.localStorage.setItem(KEY, current);
    else window.localStorage.removeItem(KEY);
  } catch {
    /* ignore: the selection still applies for this page load */
  }
  listeners.forEach((l) => l());
}

export function subscribeMemorySpace(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Test helper: forget the cached value so the next read reloads from storage. */
export function resetMemorySpaceForTests(): void {
  loaded = false;
  current = null;
  listeners.clear();
}
