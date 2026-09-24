"""Application configuration.

All external dependencies are configured through environment variables so that the
same code runs locally and on managed hosting without modification. Nothing in this
module may contain a hard-coded credential, and no connection URL defaults to a
localhost service -- see docs/adr/0004-deployment-topology.md.
"""

from __future__ import annotations

import functools

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Providers the factory knows how to build. "fake" is deliberately included so a
#: developer can run the whole system without any provider account.
_KNOWN_PROVIDERS: frozenset[str] = frozenset({"fake", "gemini", "openai", "anthropic"})

#: Embedding providers, deliberately a separate set from `_KNOWN_PROVIDERS`: an
#: LLM provider and an embedding provider are independent choices (ADR 0009), so
#: "gemini" appearing in both is a coincidence of which provider was implemented
#: first, not an assumption that they must match.
_KNOWN_EMBEDDING_PROVIDERS: frozenset[str] = frozenset({"fake", "gemini"})


class Settings(BaseSettings):
    """Environment-driven settings.

    The `.env` file is looked up both in the repository root (when the process is
    started from `backend/`) and in the current directory, so local development works
    from either location. On managed hosts no file exists and real environment
    variables are used instead.
    """

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Application -------------------------------------------------------
    app_name: str = "ai-second-brain"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    api_prefix: str = ""

    # -- HTTP / CORS -------------------------------------------------------
    # The backend is served by the platform's own process manager, which supplies
    # $PORT. Nothing in the application may assume a fixed port.
    frontend_url: str = ""
    cors_origins: str = ""
    cors_allow_vercel_previews: bool = True

    # -- Neo4j -------------------------------------------------------------
    # Required. Deliberately has no default: a silent fallback to a localhost
    # instance would make a misconfigured production deploy look healthy.
    neo4j_uri: str = ""
    neo4j_username: str = ""
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_max_connection_pool_size: int = 20
    neo4j_connection_timeout: float = 15.0
    # The driver's default transaction retry window is 30s. That is far too long for
    # a synchronous HTTP request: a request arriving during an outage would hang for
    # half a minute before returning 503. Fail fast instead and let the caller retry.
    neo4j_max_transaction_retry_time: float = 5.0

    # -- PostgreSQL (Phase 2) ----------------------------------------------
    # Application and conversation state ONLY. The canonical memory graph lives in
    # Neo4j and is never duplicated here -- see docs/adr/0006.
    # Local:        postgresql+asyncpg://localhost/ai_second_brain
    # Supabase/Neon: postgresql+asyncpg://user:pw@host/db
    # As with Neo4j, no localhost default: a silent fallback would make a
    # misconfigured production deploy look healthy.
    database_url: str = ""
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_echo: bool = False

    # -- LLM (Phase 2) -----------------------------------------------------
    # "fake" is a first-class provider: the whole system runs end to end with no
    # provider account at all, which is what keeps the test suite free.
    llm_provider: str = "fake"
    llm_model: str = ""
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    gemini_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    groq_api_key: str = ""

    # -- Extraction (Phase 2) ----------------------------------------------
    extraction_enabled: bool = True
    extraction_prompt_version: str = "v1"
    extraction_schema_version: str = "v1"
    extraction_window_size: int = Field(default=6, ge=1, le=50)
    extraction_min_message_chars: int = Field(default=25, ge=1)
    extraction_max_attempts: int = Field(default=3, ge=1, le=10)
    extraction_lease_seconds: int = Field(default=600, ge=30)
    extraction_sweep_batch_size: int = Field(default=50, ge=1, le=500)
    extraction_sweep_lookback_days: int = Field(default=7, ge=1, le=365)
    extraction_recheck_skipped: bool = True

    # -- Embeddings (Phase 3, ADR 0009) -------------------------------------
    # "fake" is a first-class provider, same rationale as LLM_PROVIDER=fake: the
    # whole system, including semantic duplicate detection and vector retrieval,
    # runs end to end with no provider account and no network access.
    embedding_provider: str = "fake"
    embedding_model: str = ""
    # The single source of truth for vector column width. Never hard-coded
    # elsewhere -- `Database.connect` asserts this matches the pgvector column's
    # declared dimension at startup (PHASE3_PLAN.md section 12), and
    # `FakeEmbeddingClient` reads this too, so "fake" vectors are the same shape
    # a real provider's would be.
    embedding_dimension: int = Field(default=768, ge=1, le=4096)
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    embedding_max_retries: int = Field(default=3, ge=0, le=10)
    # Bounded batch size for the embedding backfill sweep -- same role as
    # EXTRACTION_SWEEP_BATCH_SIZE.
    embedding_backfill_batch_size: int = Field(default=50, ge=1, le=500)

    # -- Semantic evidence thresholds (Phase 3) -----------------------------
    # Configuration, not tuned constants baked into code -- PHASE3_PLAN.md
    # section 5/16: these are expected to be adjusted against evaluation
    # results, not guessed once and left alone. Cosine similarity, [0, 1] after
    # normalization (app.semantic.similarity.cosine_similarity).
    semantic_duplicate_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    semantic_flag_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    semantic_entity_flag_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_semantic_thresholds(self) -> Settings:
        if self.semantic_flag_threshold > self.semantic_duplicate_threshold:
            raise ValueError(
                "SEMANTIC_FLAG_THRESHOLD must not exceed SEMANTIC_DUPLICATE_THRESHOLD "
                "(FLAGGED is the weaker-evidence outcome)"
            )
        return self

    # -- Hybrid retrieval ranking (Phase 3) ---------------------------------
    # An explicit, configurable linear formula (PHASE3_PLAN.md section 8.1) --
    # never a learned reranker. Weights are not normalized to sum to 1; the
    # formula is a weighted sum, and relative magnitude is what matters.
    retrieval_weight_vector: float = Field(default=1.0, ge=0.0)
    retrieval_weight_graph: float = Field(default=1.0, ge=0.0)
    retrieval_weight_keyword: float = Field(default=0.5, ge=0.0)
    retrieval_weight_recency: float = Field(default=0.2, ge=0.0)
    retrieval_weight_temporal: float = Field(default=0.5, ge=0.0)
    retrieval_weight_confidence: float = Field(default=0.3, ge=0.0)
    retrieval_top_k: int = Field(default=10, ge=1, le=200)

    # -- Multi-hop graph retrieval (Phase 4, PHASE4_PLAN.md sections 1-4) ---
    # `le=3` bakes in the approved hard maximum directly (never a value the
    # graph channel will traverse beyond, regardless of this setting);
    # `default=2` is the approved default. `decay_rate` is exponential
    # (`decay_rate ** hops`, hop 0 = 1.0) -- never a learned graph scorer.
    retrieval_graph_hops: int = Field(default=2, ge=1, le=3)
    retrieval_graph_decay_rate: float = Field(default=0.5, gt=0.0, le=1.0)
    #: Hard cap on nodes/paths a single neighbourhood traversal returns, and on
    #: distinct anchor entities the graph channel expands per query -- the
    #: explosion-prevention floor PHASE4_PLAN.md section 3 calls for even
    #: before the dense-graph benchmark decides whether more is needed.
    retrieval_graph_max_nodes: int = Field(default=200, ge=1, le=2000)

    # -- Agent (Phase 5, docs/PHASE5_PLAN.md) -------------------------------
    # Kill switch, same pattern as EXTRACTION_ENABLED.
    agent_enabled: bool = True
    #: Hard iteration cap per turn -- the agent loop's own explosion bound,
    #: same role as RETRIEVAL_GRAPH_MAX_NODES for graph traversal.
    agent_max_tool_calls: int = Field(default=6, ge=1, le=20)
    agent_tool_timeout_seconds: float = Field(default=15.0, gt=0)
    agent_turn_timeout_seconds: float = Field(default=60.0, gt=0)
    #: Total memories injected into one turn's prompt across every tool call,
    #: independent of any single search_memory call's own top_k.
    agent_max_context_memories: int = Field(default=25, ge=1, le=200)
    #: Feature-gates propose_memory_update specifically (PHASE5_PLAN.md
    #: section 7's explicit "keep AGENT write behavior feature-gated") --
    #: read-only tools are governed by agent_enabled alone.
    agent_write_enabled: bool = False

    # -- Document ingestion (Phase 6, docs/PHASE6_PLAN.md) ------------------
    # Kill switch, same pattern as EXTRACTION_ENABLED/AGENT_ENABLED.
    document_ingestion_enabled: bool = True
    #: "local" is the only real backend in v1 (zero-cost constraint) --
    #: PHASE6_PLAN.md section 10. No S3-compatible provider ships yet.
    document_storage_provider: str = "local"
    #: Root directory LocalDocumentStorage writes under. Never a path derived
    #: from user input (section 9) -- documents are keyed by a generated id.
    document_storage_dir: str = "./data/documents"
    max_document_size_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_document_pages: int = Field(default=200, ge=1, le=2000)
    max_chunks_per_document: int = Field(default=500, ge=1, le=5000)
    ingestion_parse_timeout_seconds: float = Field(default=60.0, gt=0)
    ingestion_sweep_batch_size: int = Field(default=20, ge=1, le=500)
    ingestion_lease_seconds: int = Field(default=600, ge=30)
    ingestion_max_attempts: int = Field(default=3, ge=1, le=10)
    #: Chunking (PHASE6_PLAN.md section 6) -- tunable, not a magic number,
    #: same convention as EXTRACTION_WINDOW_SIZE.
    document_chunk_size_chars: int = Field(default=2000, ge=200, le=20_000)
    document_chunk_overlap_chars: int = Field(default=200, ge=0, le=5_000)
    #: A chunk beyond a majority of pages repeating it is a running
    #: header/footer, stripped before chunking (section 5).
    document_header_footer_repetition_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    #: Below this extracted-characters-per-page floor, a PDF is treated as
    #: having no usable text layer (a scanned/image-only PDF) and fails
    #: PARSING explicitly rather than producing near-empty chunks.
    document_min_text_chars_per_page: int = Field(default=20, ge=0)
    document_extraction_prompt_version: str = "v1"

    @model_validator(mode="after")
    def _check_document_storage_provider(self) -> Settings:
        if self.document_storage_provider not in {"local"}:
            raise ValueError(
                f"DOCUMENT_STORAGE_PROVIDER must be 'local' in this phase "
                f"(zero-cost constraint, docs/PHASE6_PLAN.md section 10), "
                f"got {self.document_storage_provider!r}"
            )
        return self

    @property
    def document_extractor_version(self) -> str:
        """Idempotency key component for document-sourced extraction jobs --
        namespaced away from `extractor_version` (conversation jobs) by the
        `document:` prefix, so the two pipelines can never collide on the
        same `(target, extractor_version)` unique key even if they otherwise
        shared every component (PHASE6_PLAN.md section 3/7).
        """
        return (
            f"document:{self.llm_provider}:{self.llm_model or 'default'}:"
            f"{self.document_extraction_prompt_version}"
        )

    # -- Internal operations -----------------------------------------------
    # Guards POST /internal/extraction/sweep and POST /internal/embeddings/backfill,
    # neither of which is user-scoped.
    internal_api_token: str = ""

    # -- Graph behaviour ---------------------------------------------------
    graph_max_hops: int = Field(default=4, ge=1, le=10)
    search_max_limit: int = Field(default=200, ge=1, le=1000)

    # -- Phase 1 identity placeholder --------------------------------------
    # Phase 1 has no authentication (Phase 8). The user id arrives in the
    # X-User-Id header and falls back to this value. This is NOT a security
    # boundary; it exists so that every record is namespaced from day one and
    # real auth can replace only `app.api.deps.get_current_user_id`.
    dev_user_id: str = "dev-user"
    allow_user_id_header: bool = True

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _check_production_requirements(self) -> Settings:
        if self.app_env == "production" and self.allow_user_id_header:
            # Fail loudly rather than silently shipping a trust-the-client identity.
            raise ValueError(
                "ALLOW_USER_ID_HEADER must be false when APP_ENV=production. "
                "The X-User-Id header is a Phase 1 development placeholder and is not "
                "an authentication mechanism."
            )
        if self.llm_provider not in _KNOWN_PROVIDERS:
            raise ValueError(
                f"LLM_PROVIDER must be one of {sorted(_KNOWN_PROVIDERS)}, got {self.llm_provider!r}"
            )
        if self.embedding_provider not in _KNOWN_EMBEDDING_PROVIDERS:
            raise ValueError(
                f"EMBEDDING_PROVIDER must be one of {sorted(_KNOWN_EMBEDDING_PROVIDERS)}, "
                f"got {self.embedding_provider!r}"
            )
        return self

    @property
    def allowed_origins(self) -> list[str]:
        """Explicit CORS origin allow-list, assembled from FRONTEND_URL + CORS_ORIGINS."""
        raw = [self.frontend_url, *self.cors_origins.split(",")]
        seen: list[str] = []
        for item in raw:
            origin = item.strip().rstrip("/")
            if origin and origin not in seen:
                seen.append(origin)
        return seen

    @property
    def allowed_origin_regex(self) -> str | None:
        """Regex allowing Vercel preview deployments, which have generated hostnames."""
        if not self.cors_allow_vercel_previews:
            return None
        return r"https://.*\.vercel\.app"

    @property
    def neo4j_configured(self) -> bool:
        return bool(self.neo4j_uri and self.neo4j_username and self.neo4j_password)

    @property
    def database_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def extractor_version(self) -> str:
        """Idempotency key component.

        Encodes everything that changes what an extraction would produce, so that
        deliberately changing the prompt or model yields a new key (allowing
        re-extraction) while a retry with identical configuration does not.
        """
        return (
            f"{self.llm_provider}:{self.llm_model or 'default'}:"
            f"{self.extraction_prompt_version}:{self.extraction_schema_version}"
        )

    @property
    def llm_api_key(self) -> str:
        """The API key for the configured provider."""
        return {
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "groq": self.groq_api_key,
        }.get(self.llm_provider, "")

    @property
    def embedding_api_key(self) -> str:
        """The API key for the configured embedding provider."""
        return {"gemini": self.gemini_api_key}.get(self.embedding_provider, "")

    @property
    def embedding_model_resolved(self) -> str:
        """The exact model string that will be recorded on stored embeddings.

        Must match `app.embeddings.factory.build_embedding_client`'s construction
        exactly: `FakeEmbeddingClient` has its own default model name when
        `EMBEDDING_MODEL` is left unset, and this is the one place that default is
        named, so the backfill sweep's "does this memory already have a row under
        the *current* model" check (`app.semantic.embedding_pipeline`) can never
        drift from what a real embed call actually stores.
        """
        if self.embedding_provider == "fake":
            from app.embeddings.fake import DEFAULT_FAKE_MODEL

            return self.embedding_model or DEFAULT_FAKE_MODEL
        return self.embedding_model

    @property
    def embedding_version_key(self) -> str:
        """Identifies which `(provider, model)` a row/query belongs to (ADR 0009:
        embeddings are versioned by provider+model, never overwritten in place).
        """
        return f"{self.embedding_provider}:{self.embedding_model or 'default'}"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
