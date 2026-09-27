"""Real-LLM evaluation is explicitly opt-in (Phase 11, M3).

Before Phase 11 the agent and document runners ran their real-LLM sections
whenever `LLM_PROVIDER` was not `fake` -- so running an evaluation in a normal
developer environment whose `.env` configures Gemini spent real quota without
anyone asking for it (this happened in Phase 10). Now a real-LLM section runs
only when BOTH hold:

* a real provider is configured (`LLM_PROVIDER != "fake"`), and
* the operator asked for it: `--real-llm` on the command line, or
  `EVAL_REAL_LLM=1` in the environment (for the manual CI workflow).
"""

from __future__ import annotations

import os
import sys

from app.core.config import Settings

FLAG = "--real-llm"
ENV = "EVAL_REAL_LLM"


def real_llm_requested(settings: Settings, argv: list[str] | None = None) -> tuple[bool, str]:
    """Returns (run_real_llm, human-readable reason)."""
    args = sys.argv[1:] if argv is None else argv
    asked = FLAG in args or os.environ.get(ENV, "").strip() == "1"
    if settings.llm_provider == "fake":
        if asked:
            return False, (
                f"{FLAG}/{ENV}=1 given, but LLM_PROVIDER=fake -- configure a real provider "
                "(LLM_PROVIDER, LLM_MODEL, API key) to run the REAL-LLM section. Skipping."
            )
        return False, "LLM_PROVIDER=fake -- skipping the REAL-LLM section entirely."
    if not asked:
        return False, (
            f"LLM_PROVIDER={settings.llm_provider!r} is configured, but real-LLM evaluation is "
            f"opt-in: re-run with {FLAG} (or {ENV}=1) to spend provider quota. Skipping."
        )
    return True, (
        f"REAL-LLM section will use LLM_PROVIDER={settings.llm_provider!r} "
        f"LLM_MODEL={settings.llm_model!r} (explicitly requested)."
    )


def is_rate_limit_error(error: str) -> bool:
    """A real-LLM task error caused by provider throttling. Type-based since
    Phase 11 (`LLMRateLimitedError`); the `"429"` substring is kept so results
    persisted by earlier runs are still classified when a run resumes."""
    return "LLMRateLimitedError" in error or "rate limit" in error.lower() or "429" in error
