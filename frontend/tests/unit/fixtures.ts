/** Typed test fixtures shaped exactly like the generated OpenAPI schemas. */
import type { GraphSlice, GraphSliceEdge, GraphSliceNode, Memory, MemoryDetail } from "@/lib/api/types";
import type { Transport } from "@/lib/api/client";

export function node(id: string, extra: Partial<GraphSliceNode> = {}): GraphSliceNode {
  return { id, kind: "entity", label: id, ...extra };
}

export function edge(id: string, source: string, target: string, extra: Partial<GraphSliceEdge> = {}): GraphSliceEdge {
  return { id, source, target, kind: "relates_to", predicate: "uses", ...extra };
}

export function slice(extra: Partial<GraphSlice> = {}): GraphSlice {
  return {
    as_of: "2026-06-01T00:00:00Z",
    budget: { max_nodes: 150, max_edges: 300 },
    redirect_depth: 0,
    temporal_scope: "current",
    truncated_nodes: false,
    truncated_edges: false,
    truncated_memories: false,
    nodes: [],
    edges: [],
    ...extra,
  };
}

export function memory(id: string, extra: Partial<Memory> = {}): Memory {
  return {
    id,
    user_id: "u",
    content: `memory ${id}`,
    memory_type: "semantic",
    status: "active",
    confidence: 0.9,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    valid_from: "2026-01-01T00:00:00Z",
    valid_until: null,
    ...extra,
  } as Memory;
}

export function memoryDetail(id: string, extra: Partial<Memory> = {}): MemoryDetail {
  return { memory: memory(id, extra), entities: [], relationships: [] } as unknown as MemoryDetail;
}

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

/** A fake transport that records calls and answers from a route table. */
export function fakeTransport(routes: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const calls: { url: string; init: RequestInit }[] = [];
  const transport: Transport = async (url, init) => {
    calls.push({ url, init });
    return routes(url, init);
  };
  return { transport, calls };
}
