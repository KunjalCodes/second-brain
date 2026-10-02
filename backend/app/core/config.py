"""Application configuration.

All external dependencies are configured through environment variables so that the
same code runs locally and on managed hosting without modification. Nothing in this
module may contain a hard-coded credential, and no connection URL defaults to a
localhost service -- see docs/adr/0004-deployment-topology.md.
"""

from __future__ import annotations

import functools
from typing import Literal

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
    #: Phase 9: opt-in, and only for ONE Vercel project (`CORS_VERCEL_PROJECT`).
    #: The pre-Phase-9 default admitted every `*.vercel.app` site -- any
    #: stranger's deployment -- as a credentialed origin (PHASE9_PLAN.md F7).
    cors_allow_vercel_previews: bool = False
    cors_vercel_project: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9-]{0,62})?$")
    #: Phase 11 (S7): the Vercel team/account slug that preview hostnames end
    #: with. Required with previews; previews are refused outright in
    #: production-like environments (a `*.vercel.app` pattern can be matched by
    #: someone else's project name).
    cors_vercel_team_scope: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9-]{0,62})?$")
    #: Phase 11 (D8): interactive API docs (/docs, /redoc, /openapi.json).
    #: Empty = on in development/test, OFF in production-like environments.
    expose_docs: bool | None = None
    #: Phase 11 (D7): explicit opt-in to fake/offline AI providers in a
    #: production-like environment (the staging default). Without it,
    #: LLM_PROVIDER=fake / EMBEDDING_PROVIDER=fake refuse to start there.
    allow_fake_ai: bool = False
    #: Phase 11 (M5): `json` (default in production-like environments) or `text`.
    log_format: str = Field(default="", pattern=r"^(|json|text)$")

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
    #: How long a background job may keep waiting out provider throttling
    #: (HTTP 429 / cooldown) without spending retry attempts, measured from job
    #: creation. Past this, throttling counts as an ordinary failed attempt so
    #: the job still reaches a terminal state (Phase 11, section 12).
    llm_throttle_max_age_seconds: int = Field(default=86400, ge=60, le=7 * 86400)
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
    #: Upper bound on memories scanned per user per backfill tick (Phase 11).
    embedding_backfill_max_scan: int = Field(default=10000, ge=100, le=1_000_000)

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
    #: Phase 11 (S10): cap on every non-upload request body (JSON). Uploads
    #: are capped at MAX_DOCUMENT_SIZE_BYTES plus multipart overhead.
    max_request_body_bytes: int = Field(default=1024 * 1024, ge=1024)
    max_document_pages: int = Field(default=200, ge=1, le=2000)
    max_chunks_per_document: int = Field(default=500, ge=1, le=5000)
    ingestion_parse_timeout_seconds: float = Field(default=60.0, gt=0)
    #: Staging storage policy (docs/PHASE11_PLAN.md section 16): delete an
    #: upload's raw bytes once its chunks exist. Nothing reads them afterwards
    #: (provenance is served from PostgreSQL), and it bounds disk use on an
    #: ephemeral filesystem. Off by default, so local behaviour is unchanged.
    document_delete_bytes_after_ingestion: bool = False
    ingestion_sweep_batch_size: int = Field(default=20, ge=1, le=500)
    ingestion_lease_seconds: int = Field(default=600, ge=30)
    ingestion_max_attempts: int = Field(default=3, ge=1, le=10)

    # -- In-process sweeper (Phase 11, app/workers/sweeper.py) ---------------
    #: Seconds between recovery passes; 0 disables the sweeper (tests, or a
    #: deployment that triggers the internal sweep endpoints externally).
    sweep_interval_seconds: float = Field(default=60.0, ge=0, le=3600)
    sweep_initial_delay_seconds: float = Field(default=5.0, ge=0, le=600)
    sweep_shutdown_grace_seconds: float = Field(default=15.0, ge=0, le=120)
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
    #: Phase 8 (D1, Stage 0-measured): caps on GET /entities/{id}/graph, which
    #: had none -- at 10k synthetic entities one request returned ~89 MB. The
    #: defaults are the largest result size Stage 0 measured as healthy locally
    #: (~1k nodes / ~4k edges in <=1.3 s); `Neighborhood.truncated` reports a cut.
    graph_max_nodes: int = Field(default=1000, ge=1, le=10_000)
    graph_max_edges: int = Field(default=5000, ge=1, le=50_000)
    search_max_limit: int = Field(default=200, ge=1, le=1000)

    # -- Memory graph exploration API (Phase 8, docs/PHASE8_PLAN.md section 8.3) --
    # Server-side budgets for /graph/*. A client may lower them per request but
    # never raise them past these values; each `le=` is the approved hard cap.
    graph_explore_max_nodes: int = Field(default=150, ge=1, le=1000)
    graph_explore_max_edges: int = Field(default=400, ge=1, le=3000)
    graph_explore_max_hops: int = Field(default=2, ge=1, le=3)
    graph_explore_max_memories_per_entity: int = Field(default=5, ge=1, le=25)
    # -- Graph intelligence (Phase 15, docs/PHASE15_GRAPH_INTELLIGENCE.md) -------
    # Same contract: a client may lower these, never raise them; `le=` is the
    # hard cap. Path search also stays inside `graph_explore_max_nodes`.
    graph_path_max_hops: int = Field(default=3, ge=1, le=4)
    graph_path_max_paths: int = Field(default=5, ge=1, le=20)
    graph_context_max_nodes: int = Field(default=25, ge=1, le=60)
    graph_context_max_evidence: int = Field(default=8, ge=1, le=20)
    graph_context_min_vector_similarity: float = Field(default=0.5, ge=0.0, le=1.0)
    entity_context_max_relationships: int = Field(default=25, ge=1, le=100)
    entity_context_max_related: int = Field(default=10, ge=1, le=50)
    entity_context_max_memories: int = Field(default=10, ge=1, le=50)

    # -- Phase 16: projects (docs/PHASE16_ARCHITECTURE.md) ------------------------------
    #: A project is a memory space owned by one account. Caps keep a runaway
    #: client from creating unbounded spaces or credentials.
    projects_max_per_user: int = Field(default=50, ge=1, le=1000)
    project_tokens_max_per_project: int = Field(default=10, ge=1, le=100)
    #: Project tokens always expire; the default is 90 days, never longer than a year.
    project_token_default_ttl_days: int = Field(default=90, ge=1, le=365)
    #: Agent events: an event may not claim to be from the future (clock skew only) or
    #: from long ago, and a session may not grow without bound.
    project_event_max_future_seconds: int = Field(default=300, ge=0, le=3600)
    project_event_max_age_days: int = Field(default=365, ge=1, le=3650)
    project_events_max_per_session: int = Field(default=2000, ge=1, le=100_000)
    #: A projection that keeps failing (e.g. Neo4j down) is retried this many times.
    project_projection_max_attempts: int = Field(default=10, ge=1, le=1000)

    # -- Identity (Phase 9, docs/PHASE9_PLAN.md sections 6, 12, 13) -------------
    #: `session` (default): identity comes ONLY from a verified session
    #: credential. `dev_header`: the pre-Phase-9 `X-User-Id` / `DEV_USER_ID`
    #: placeholder -- development and tests only; refused in any production-like
    #: environment. None = not set explicitly (see `effective_auth_mode`).
    auth_mode: Literal["session", "dev_header"] | None = None
    #: Deprecated compatibility alias: `true` == `AUTH_MODE=dev_header`.
    #: Setting both to contradicting values refuses to start.
    allow_user_id_header: bool | None = None
    #: Fallback identity in dev_header mode when no header is sent.
    dev_user_id: str = "dev-user"

    #: Self-service registration. None = on in development/test, OFF in any
    #: production-like environment (accounts come from scripts/manage_users.py).
    auth_registration_enabled: bool | None = None
    auth_session_ttl_hours: int = Field(default=168, ge=1, le=720)
    auth_session_idle_hours: int = Field(default=24, ge=1, le=720)
    #: `last_used_at` is written at most this often (bounds per-request writes).
    auth_session_touch_seconds: int = Field(default=300, ge=0, le=3600)
    auth_cookie_name: str = Field(default="sb_session", pattern=r"^[A-Za-z0-9_]{1,64}$")
    #: None = Secure off only in development/test, ON everywhere else.
    auth_cookie_secure: bool | None = None
    auth_cookie_domain: str = ""
    #: scrypt cost. Default (N=2^15, r=8, p=3; 32 MiB) is an OWASP-listed
    #: configuration, measured at ~290 ms per hash on the development machine;
    #: N=2^17/r=8/p=1 measured ~420 ms there. Production-like environments must
    #: meet `SCRYPT_FLOOR`; tests may lower it for speed.
    auth_scrypt_n: int = Field(default=2**15, ge=2**4, le=2**20)
    auth_scrypt_r: int = Field(default=8, ge=1, le=32)
    auth_scrypt_p: int = Field(default=3, ge=1, le=16)
    auth_scrypt_maxmem: int = Field(default=64 * 1024 * 1024, ge=1024 * 1024, le=2**31)
    #: Comma-separated IPs/CIDRs of reverse proxies whose X-Forwarded-For is
    #: trusted for client-IP rate limiting. Empty = trust no forwarded header.
    trusted_proxy_ips: str = ""

    # -- Rate limiting (Phase 9, docs/PHASE9_PLAN.md section 11) ---------------
    # Fixed windows in PostgreSQL; counts are per window. Windows are fixed per
    # class (see app/services/rate_limit_service.py); limits are configurable.
    rate_limit_enabled: bool = True
    rate_limit_login_per_ip: int = Field(default=10, ge=1, le=10_000)  # / 15 min
    rate_limit_login_per_email: int = Field(default=5, ge=1, le=10_000)  # / 15 min
    rate_limit_register_per_ip: int = Field(default=5, ge=1, le=10_000)  # / hour
    #: Phase 11 (D5). True only when `client_ip()` sees the REAL client
    #: address (direct connection, or a proxy chain in TRUSTED_PROXY_IPS that
    #: is known to overwrite X-Forwarded-For). Behind the Next.js rewrite /
    #: a platform load balancer it sees the proxy, so per-IP buckets would be
    #: one bucket shared by every user; set false there and the explicit
    #: global ceilings below apply instead. Per-email and per-user limits are
    #: unaffected either way.
    rate_limit_per_ip: bool = True
    rate_limit_login_global: int = Field(default=300, ge=1, le=100_000)  # / 15 min
    rate_limit_register_global: int = Field(default=50, ge=1, le=100_000)  # / hour
    rate_limit_graph_per_user: int = Field(default=120, ge=1, le=100_000)  # / minute
    rate_limit_search_per_user: int = Field(default=120, ge=1, le=100_000)  # / minute
    rate_limit_agent_per_user: int = Field(default=30, ge=1, le=100_000)  # / hour
    rate_limit_message_per_user: int = Field(default=120, ge=1, le=100_000)  # / hour
    rate_limit_ingest_per_user: int = Field(default=20, ge=1, le=100_000)  # / hour
    rate_limit_write_per_user: int = Field(default=120, ge=1, le=100_000)  # / minute

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator(
        "auth_mode",
        "allow_user_id_header",
        "auth_registration_enabled",
        "auth_cookie_secure",
        mode="before",
    )
    @classmethod
    def _empty_means_unset(cls, v: object) -> object:
        """`AUTH_MODE=` (empty, as in .env.example) means "not set": use the
        environment-dependent default rather than failing to parse."""
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("expose_docs", mode="before")
    @classmethod
    def _empty_docs_means_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("trusted_proxy_ips")
    @classmethod
    def _valid_proxy_networks(cls, v: str) -> str:
        import ipaddress

        for item in v.split(","):
            if item.strip():
                ipaddress.ip_network(item.strip(), strict=False)  # ValueError -> refuse
        return v

    @model_validator(mode="after")
    def _check_production_requirements(self) -> Settings:
        if self.auth_mode is not None and self.allow_user_id_header is not None:
            wants_header = self.auth_mode == "dev_header"
            if wants_header != self.allow_user_id_header:
                raise ValueError(
                    "AUTH_MODE and ALLOW_USER_ID_HEADER contradict each other; set only "
                    "AUTH_MODE (ALLOW_USER_ID_HEADER is a deprecated alias)."
                )
        if self.cors_allow_vercel_previews and not (
            self.cors_vercel_project and self.cors_vercel_team_scope
        ):
            raise ValueError(
                "CORS_ALLOW_VERCEL_PREVIEWS=true requires CORS_VERCEL_PROJECT=<project slug> and "
                "CORS_VERCEL_TEAM_SCOPE=<team slug>: admitting every *.vercel.app site as a "
                "credentialed origin is unsafe."
            )
        if self.is_production_like:
            # Fail loudly rather than silently shipping a trust-the-client identity
            # or a weak browser boundary (PHASE9_PLAN.md section 12). Any APP_ENV
            # other than development/test counts -- a typo is not a bypass.
            env = f"APP_ENV={self.app_env!r}"
            if self.allow_user_id_header:
                raise ValueError(
                    f"ALLOW_USER_ID_HEADER must be false when {env}. The X-User-Id header "
                    "is a development placeholder and is not an authentication mechanism."
                )
            if self.effective_auth_mode != "session":
                raise ValueError(f"AUTH_MODE must be 'session' when {env}.")
            if not self.cookie_secure:
                raise ValueError(f"AUTH_COOKIE_SECURE must be true when {env}.")
            if not self.allowed_origins:
                raise ValueError(
                    f"FRONTEND_URL or CORS_ORIGINS must list the browser origin(s) when {env}: "
                    "the CSRF Origin check and credentialed CORS need an explicit allow-list."
                )
            if not self.scrypt_meets_floor:
                raise ValueError(
                    f"AUTH_SCRYPT_N/R/P are below the OWASP floor when {env} "
                    "(need N>=2^15, r>=8 and N*r*p >= 2^15*8*3)."
                )
            self._check_deployment_requirements(env)
        if 128 * self.auth_scrypt_n * self.auth_scrypt_r * 2 > self.auth_scrypt_maxmem:
            raise ValueError("AUTH_SCRYPT_MAXMEM is too small for AUTH_SCRYPT_N/R.")
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

    def _check_deployment_requirements(self, env: str) -> None:
        """Phase 11 (M4, docs/PHASE11_PLAN.md section 23): refuse to start a
        production-like process on insecure or broken development defaults,
        instead of discovering them at the first request or job."""
        problems: list[str] = []
        if not self.database_url:
            problems.append("DATABASE_URL must be set")
        if not self.neo4j_configured:
            problems.append("NEO4J_URI, NEO4J_USERNAME and NEO4J_PASSWORD must be set")
        if len(self.internal_api_token) < 32:
            problems.append(
                "INTERNAL_API_TOKEN must be set to a random value of at least 32 characters"
            )
        fake = [
            name
            for name, provider in (
                ("LLM_PROVIDER", self.llm_provider),
                ("EMBEDDING_PROVIDER", self.embedding_provider),
            )
            if provider == "fake"
        ]
        if fake and not self.allow_fake_ai:
            problems.append(
                f"{' and '.join(fake)}=fake: set ALLOW_FAKE_AI=true to run with the offline "
                "providers deliberately (e.g. staging), or configure a real provider"
            )
        if self.llm_provider not in {"fake", "gemini"}:
            problems.append(f"LLM_PROVIDER={self.llm_provider!r} has no adapter yet")
        if self.llm_provider == "gemini" and not (self.gemini_api_key and self.llm_model):
            problems.append("LLM_PROVIDER=gemini requires GEMINI_API_KEY and LLM_MODEL")
        if self.embedding_provider == "gemini" and not (
            self.embedding_api_key and self.embedding_model
        ):
            problems.append("EMBEDDING_PROVIDER=gemini requires an API key and EMBEDDING_MODEL")
        if not self.document_storage_dir.startswith("/"):
            problems.append(
                "DOCUMENT_STORAGE_DIR must be an absolute path (a relative path depends on "
                "the working directory the host happens to use)"
            )
        if self.debug:
            problems.append("DEBUG must be false")
        if self.database_echo:
            problems.append("DATABASE_ECHO must be false (it logs every SQL statement)")
        if self.log_level == "DEBUG":
            problems.append("LOG_LEVEL must not be DEBUG")
        if self.cors_allow_vercel_previews:
            problems.append(
                "CORS_ALLOW_VERCEL_PREVIEWS must be false: preview hostnames cannot be "
                "distinguished from other people's Vercel projects by pattern"
            )
        for origin in self.allowed_origins:
            host = origin.split("://", 1)[-1].split(":", 1)[0]
            if not origin.startswith("https://") and host not in {"localhost", "127.0.0.1"}:
                problems.append(f"browser origin {origin!r} must use https://")
        if problems:
            raise ValueError(
                f"Refusing to start with {env}:\n  - " + "\n  - ".join(problems)
            )

    @property
    def docs_enabled(self) -> bool:
        if self.expose_docs is not None:
            return self.expose_docs
        return not self.is_production_like

    @property
    def effective_log_format(self) -> str:
        if self.log_format:
            return self.log_format
        return "json" if self.is_production_like else "text"

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
        """Regex allowing ONE Vercel project's preview deployments (generated
        hostnames `<project>-<hash>-<scope>.vercel.app`). Never all of vercel.app."""
        if not (
            self.cors_allow_vercel_previews
            and self.cors_vercel_project
            and self.cors_vercel_team_scope
        ):
            return None
        project, scope = self.cors_vercel_project, self.cors_vercel_team_scope
        # Only Vercel's two preview shapes, both ending in THIS team's scope:
        #   <project>-<9-char hash>-<scope>.vercel.app
        #   <project>-git-<branch>-<scope>.vercel.app
        # (Development only -- production-like environments refuse previews.)
        return rf"https://{project}-(?:[a-z0-9]{{9}}|git-[a-z0-9-]+)-{scope}\.vercel\.app"

    # -- Phase 9 derived identity/security settings ---------------------------

    @property
    def is_production_like(self) -> bool:
        return self.app_env not in {"development", "test"}

    @property
    def effective_auth_mode(self) -> str:
        if self.auth_mode is not None:
            return self.auth_mode
        return "dev_header" if self.allow_user_id_header else "session"

    @property
    def registration_enabled(self) -> bool:
        if self.auth_registration_enabled is not None:
            return self.auth_registration_enabled
        return not self.is_production_like

    @property
    def cookie_secure(self) -> bool:
        if self.auth_cookie_secure is not None:
            return self.auth_cookie_secure
        return self.is_production_like

    @property
    def scrypt_meets_floor(self) -> bool:
        n, r, p = self.auth_scrypt_n, self.auth_scrypt_r, self.auth_scrypt_p
        return n >= 2**15 and r >= 8 and n * r * p >= 2**15 * 8 * 3

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
