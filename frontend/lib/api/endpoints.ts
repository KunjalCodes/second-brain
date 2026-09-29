/**
 * Typed wrappers around the EXISTING backend endpoints (docs/API.md). One
 * function per endpoint the UI uses -- no invented endpoints, no client-side
 * business logic. Parameter names are the backend's own.
 */
import { apiRequest, TIMEOUTS } from "./client";
import type { components } from "./schema";
import type * as T from "./types";

type S = components["schemas"];
const enc = encodeURIComponent;

// -- auth -----------------------------------------------------------------------
export const getMe = (signal?: AbortSignal) => apiRequest<T.MeResponse>("/auth/me", { signal });
export const login = (email: string, password: string) =>
  apiRequest<T.LoginResponse>("/auth/login", { method: "POST", json: { email, password } });
export const logout = () => apiRequest<void>("/auth/logout", { method: "POST" });
export const logoutAll = () => apiRequest<void>("/auth/logout-all", { method: "POST" });
export const changePassword = (current_password: string, new_password: string) =>
  apiRequest<void>("/auth/password", { method: "POST", json: { current_password, new_password } });
export const register = (email: string, password: string) =>
  apiRequest<S["RegisterResponse"]>("/auth/register", { method: "POST", json: { email, password } });

// -- memories ---------------------------------------------------------------------
export interface MemorySearchParams {
  q?: string;
  status?: T.MemoryStatus[];
  memory_type?: T.MemoryType[];
  entity?: string;
  as_of?: string;
  include_excluded?: boolean;
  limit?: number;
  offset?: number;
}
export const searchMemories = (p: MemorySearchParams, signal?: AbortSignal) =>
  apiRequest<T.MemoryListResponse>("/memories/search", { query: { ...p }, signal });
export const getMemory = (id: string, signal?: AbortSignal) =>
  apiRequest<T.MemoryDetail>(`/memories/${enc(id)}`, { signal });
export const getProvenance = (id: string, signal?: AbortSignal) =>
  apiRequest<T.ProvenanceView>(`/memories/${enc(id)}/provenance`, { signal });
export const getMemoryExplanation = (id: string, signal?: AbortSignal) =>
  apiRequest<T.MemoryExplanation>(`/memories/${enc(id)}/explanation`, { signal });
export const invalidateMemory = (id: string, reason: string | null) =>
  apiRequest<T.Memory>(`/memories/${enc(id)}/invalidate`, { method: "POST", json: { reason } });

// -- hybrid search --------------------------------------------------------------------
export const hybridSearch = (query: string, limit = 50, signal?: AbortSignal) =>
  apiRequest<T.SearchResponse>("/search", { method: "POST", json: { query, limit }, signal });

// -- entities ---------------------------------------------------------------------------
export const searchEntities = (q: string, limit = 25, offset = 0, signal?: AbortSignal) =>
  apiRequest<T.EntityListResponse>("/entities/search", { query: { q, limit, offset }, signal });
export const getEntity = (id: string, signal?: AbortSignal) =>
  apiRequest<T.EntityDetail>(`/entities/${enc(id)}`, { signal });
export const getEntityHistory = (id: string, asOf?: string, signal?: AbortSignal) =>
  apiRequest<T.EntityStateView>(`/entities/${enc(id)}/history`, { query: { as_of: asOf }, signal });
export const getEntityMemories = (id: string, limit = 25, offset = 0, signal?: AbortSignal) =>
  apiRequest<T.Memory[]>(`/entities/${enc(id)}/memories`, { query: { limit, offset }, signal });

// -- graph (Phase 8) ----------------------------------------------------------------------
export interface GraphScopeParams {
  temporal_scope?: T.TemporalScope;
  as_of?: string;
  include?: T.GraphInclude[];
}
export const graphOverview = (p: GraphScopeParams & { entity_type?: T.EntityType[] }, signal?: AbortSignal) =>
  apiRequest<T.GraphSlice>("/graph/overview", { query: { ...p }, signal });
export const graphNeighborhood = (
  id: string,
  p: GraphScopeParams & { hops?: number; exclude_ids?: string[] },
  signal?: AbortSignal,
) => apiRequest<T.GraphSlice>(`/graph/entities/${enc(id)}/neighborhood`, { query: { ...p }, signal });
export const edgeEvidence = (
  p: { source_entity_id: string; target_entity_id: string; predicate: string } & GraphScopeParams,
  signal?: AbortSignal,
) => apiRequest<T.EdgeEvidence>("/graph/edges/evidence", { query: { ...p }, signal });
export const memoryTimeline = (id: string, signal?: AbortSignal) =>
  apiRequest<T.GraphSlice>(`/graph/memories/${enc(id)}/timeline`, { signal });

// -- documents ------------------------------------------------------------------------------
export const listDocuments = (limit = 25, offset = 0, signal?: AbortSignal) =>
  apiRequest<T.DocumentListResponse>("/documents", { query: { limit, offset }, signal });
export const getDocument = (id: string, signal?: AbortSignal) =>
  apiRequest<T.DocumentResponse>(`/documents/${enc(id)}`, { signal });
export const getDocumentChunks = (id: string, signal?: AbortSignal) =>
  apiRequest<S["DocumentChunkListResponse"]>(`/documents/${enc(id)}/chunks`, { signal });
export function uploadDocument(file: File): Promise<T.DocumentResponse> {
  const form = new FormData();
  form.append("file", file);
  return apiRequest<T.DocumentResponse>("/documents", { method: "POST", form, timeoutMs: TIMEOUTS.upload });
}

// -- conversations / agent ----------------------------------------------------------------------
export const listConversations = (limit = 25, offset = 0, signal?: AbortSignal) =>
  apiRequest<T.ConversationListResponse>("/conversations", { query: { limit, offset }, signal });
export const getConversation = (id: string, signal?: AbortSignal) =>
  apiRequest<T.ConversationDetail>(`/conversations/${enc(id)}`, { signal });
export const createConversation = (title: string | null) =>
  apiRequest<T.Conversation>("/conversations", { method: "POST", json: { title } });
export const agentTurn = (conversationId: string, message: string) =>
  apiRequest<T.AgentTurnResponse>(`/conversations/${enc(conversationId)}/agent-turn`, {
    method: "POST",
    json: { message },
    timeoutMs: TIMEOUTS.agent,
  });

// -- extraction status --------------------------------------------------------------------------
export const listExtractionJobs = (limit = 25, offset = 0, signal?: AbortSignal) =>
  apiRequest<T.ExtractionJobListResponse>("/extraction/jobs", { query: { limit, offset }, signal });

// -- review / governance (Phase 4) ------------------------------------------------------------------
export const listMemoryReview = (limit = 25, offset = 0, signal?: AbortSignal) =>
  apiRequest<T.MemoryReviewCandidateListResponse>("/review/memories", { query: { limit, offset }, signal });
export const getMemoryReview = (id: string, signal?: AbortSignal) =>
  apiRequest<T.MemoryReviewCandidateDetail>(`/review/memories/${enc(id)}`, { signal });
export const confirmDuplicate = (id: string, if_version: number, reason: string | null) =>
  apiRequest<T.MemoryReviewCandidateDetail>(`/review/memories/${enc(id)}/confirm-duplicate`, {
    method: "POST",
    json: { if_version, reason },
  });
export const confirmDistinct = (id: string, if_version: number, reason: string | null) =>
  apiRequest<T.MemoryReviewCandidateDetail>(`/review/memories/${enc(id)}/confirm-distinct`, {
    method: "POST",
    json: { if_version, reason },
  });
export const resolveConflict = (
  id: string,
  body: { if_version: number; resolution: "supersede" | "keep_both" | "reject"; winner_memory_id?: string | null; reason: string | null },
) =>
  apiRequest<T.MemoryReviewCandidateDetail>(`/review/memories/${enc(id)}/resolve-conflict`, {
    method: "POST",
    json: body,
  });
export const listEntityReview = (limit = 25, offset = 0, signal?: AbortSignal) =>
  apiRequest<T.EntityMergeCandidateListResponse>("/review/entities", { query: { limit, offset }, signal });
export const getEntityReview = (id: string, signal?: AbortSignal) =>
  apiRequest<T.EntityMergeCandidateDetail>(`/review/entities/${enc(id)}`, { signal });
export const approveMerge = (id: string, if_version: number, canonical_entity_id: string, reason: string | null) =>
  apiRequest<T.EntityMergeCandidateDetail>(`/review/entities/${enc(id)}/approve-merge`, {
    method: "POST",
    json: { if_version, canonical_entity_id, reason },
  });
export const rejectMerge = (id: string, if_version: number, reason: string | null) =>
  apiRequest<T.EntityMergeCandidateDetail>(`/review/entities/${enc(id)}/reject-merge`, {
    method: "POST",
    json: { if_version, reason },
  });
export const reverseMerge = (id: string, if_version: number, reason: string | null) =>
  apiRequest<T.EntityMergeCandidateDetail>(`/review/entities/${enc(id)}/reverse-merge`, {
    method: "POST",
    json: { if_version, reason },
  });
export const reviewHistory = (limit = 25, offset = 0, signal?: AbortSignal) =>
  apiRequest<T.ReviewAuditLogResponse>("/review/history", { query: { limit, offset }, signal });
