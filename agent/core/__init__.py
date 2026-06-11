# Core module
from .event_bus import EventBus, Event
from .embeddings import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    SentenceTransformerProvider,
    TfidfEmbeddingProvider,
    get_default_provider,
)
from .hooks import (
    HookRegistry,
    STANDARD_HOOKS,
    BEFORE_PERCEIVE,
    BEFORE_LLM_CALL,
    AFTER_LLM_CALL,
    BEFORE_DECIDE,
    BEFORE_TOOL_EXECUTION,
    AFTER_TOOL_EXECUTION,
    ON_ERROR,
    ON_TOKEN,
    BEFORE_COMPACT,
    AFTER_COMPACT,
    ON_SESSION_END,
)
from .dual_review import (
    DualReviewManager,
    DualReviewResult,
    PermissionDenied,
    RateLimiter,
    ReviewDecision,
    ReviewRequiresUser,
    ReviewVerdict,
    get_dual_review_manager,
    reset_dual_review_manager,
)
from .progress_anchor import (
    ProgressAnchor,
    ProgressRecord,
    load_progress,
)

__all__ = [
    "EventBus", "Event", "HookRegistry", "STANDARD_HOOKS",
    "BEFORE_PERCEIVE", "BEFORE_LLM_CALL", "AFTER_LLM_CALL", "BEFORE_DECIDE",
    "BEFORE_TOOL_EXECUTION", "AFTER_TOOL_EXECUTION", "ON_ERROR", "ON_TOKEN",
    "BEFORE_COMPACT", "AFTER_COMPACT", "ON_SESSION_END",
    # PR-04: embedding providers
    "EmbeddingProvider", "HashingEmbeddingProvider",
    "SentenceTransformerProvider", "TfidfEmbeddingProvider",
    "get_default_provider",
    # PR-11: dual-agent review
    "DualReviewManager", "DualReviewResult",
    "PermissionDenied", "ReviewRequiresUser",
    "ReviewDecision", "ReviewVerdict", "RateLimiter",
    "get_dual_review_manager", "reset_dual_review_manager",
    # PR-13: progress anchor
    "ProgressAnchor", "ProgressRecord", "load_progress",
]