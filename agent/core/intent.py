"""Intent classification + routing — LLM-based with heuristic fallback (PR-15→17).

This module maps a user request to one of {ask, edit, agent} and
dispatches to the registered handler.

PR-15 refactor:
  - `IntentClassifier` now inherits from `LLMExtractor[str]`
  - LLM is the primary classifier (JSON schema in system prompt)
  - Heuristic (`_legacy_extract`) is a minimal offline fallback.
  - Cache + JSON parsing + fallback chain live in the base class.

PR-17 consolidation:
  - Removed the local `_IntentCache` class. Cache is now the single
    `LLMExtractor._cache` (TTL-bounded LRU, same behavior). PR-14's
    `cache` parameter to `IntentClassifier.__init__` is now a no-op
    (silently ignored) — present only to not break external callers.
  - The `cache` property is removed (it returned the legacy cache).
    Use `cache_stats()` instead (same dict shape).

Public API (current):
  - `classify(task) -> "ask"|"edit"|"agent"`  (async, PR-14)
  - `IntentRouter.register(intent, handler)`
  - `INTENT_ASK/EDIT/AGENT` — `Intent` dataclass constants
  - `CLASSIFY_PROMPT` — kept for backward compat (reference)
"""

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .llm_extractor import LLMExtractor

# ── Intent definitions ──────────────────────────────────────────


@dataclass(frozen=True)
class Intent:
    name: str  # e.g. "ask", "edit", "agent"
    description: str  # for the classification prompt


# Built-in intents
INTENT_ASK = Intent(
    "ask", "A conversational question, explanation, or chat — no file changes needed"
)
INTENT_EDIT = Intent(
    "edit",
    "A single operation: install a package, rename a symbol, fix a specific bug in one file, or run a command",
)
INTENT_AGENT = Intent(
    "agent",
    "A multi-step project task: build a feature, create multiple files, refactor across files",
)


# All built-in intent names (used for fallback parse and validation)
_BUILTIN_INTENT_NAMES = ("ask", "edit", "agent")


# ── Legacy classify prompt (PR-14, kept for backward compat) ──

CLASSIFY_PROMPT = """\
Classify the user's request into exactly one of these categories:

ask   — {ask_desc}
edit  — {edit_desc}
agent — {agent_desc}

Reply with exactly one word: ask, edit, or agent. No explanation.

User request: "{task}"
Intent:"""


# ── LLM-based classifier (PR-15 + PR-17) ──────────────────────


class IntentClassifier(LLMExtractor[str]):
    """LLM-first intent classifier with minimal heuristic fallback.

    Tier 1: cache (LLMExtractor base — TTL-bounded LRU)
    Tier 2: LLM with JSON output: {"intent": "edit", "confidence": 0.9, ...}
    Tier 3: minimal legacy — greetings + self-referential only

    Public API (PR-14 compatible):
      - `classify(task)` — async, returns intent name
      - `cache_stats()` — same dict shape as the old `_IntentCache.stats()`
      - `using_llm` property — True if LLM path is active

    The `cache` constructor parameter is accepted but IGNORED (PR-17).
    Use `cache_ttl` to configure the LLMExtractor's cache.
    """

    INTENT_SCHEMA: dict = {
        "ask": INTENT_ASK.description,
        "edit": INTENT_EDIT.description,
        "agent": INTENT_AGENT.description,
    }

    def __init__(
        self,
        llm_client=None,
        use_llm: bool = True,
        fallback_to_legacy: bool = True,
        cache=None,  # PR-17: ignored, kept for PR-14 backward compat
        cache_ttl: float = 300.0,
    ):
        super().__init__(
            llm_client=llm_client,
            use_llm=use_llm,
            fallback_to_legacy=fallback_to_legacy,
            cache_ttl=cache_ttl,
        )

    # ── LLMExtractor implementation ─────────────────────────────

    def _system_prompt(self) -> str:
        schema = json.dumps(self.INTENT_SCHEMA, indent=2, ensure_ascii=False)
        return f"""You are a request router. Classify the user's request into exactly one intent.

INTENT SCHEMA:
{schema}

OUTPUT: Reply with JSON only, e.g.
  {{"intent": "edit", "confidence": 0.9, "reasoning": "user wants to install a package"}}
No markdown, no explanation outside the JSON.

GUIDANCE:
  - "ask" — pure chat, no file change implied
  - "edit" — single, well-defined file operation (install/rename/fix one bug)
  - "agent" — multi-step or unclear scope (build features, refactor across files)"""

    def _legacy_extract(self, task: str) -> str:
        """Offline keyword fallback (PR-14 rules restored). Reached only when
        the LLM is unavailable (``use_llm=False``) or the LLM call fails —
        never in the primary LLM path. Pure function: no I/O, no network.

        First-match-wins order:
          1. empty → "agent"
          2. greeting → "ask"
          3. self-referential → "ask"
          4. question phrase (how/why/是什么/怎么办) → "ask"
          5. edit trigger (install/fix/rename/run-tests) → "edit"
          6. else → "agent" (safe default; LLM resolves ambiguity normally)

        The edit triggers are deliberately multi-word where ambiguous:
        ``"run test"`` matches but bare ``"run"`` does NOT (a bare "run" is
        too ambiguous to assume a single-step edit).
        """
        t = (task or "").strip().lower()
        if not t:
            return "agent"
        # Greetings (covers most "short ask" cases)
        if any(
            t.startswith(g)
            for g in (
                "hello",
                "hi",
                "hi ",
                "hi.",
                "hey",
                "你好",
                "您好",
                "早上好",
                "晚上好",
                "嗨",
            )
        ) or t in ("hi", "hello", "hey", "你好"):
            return "ask"
        # Self-referential
        if any(
            p in t
            for p in (
                "你是谁",
                "who are you",
                "what can you do",
                "你能做什么",
                "can you do",
                "help me understand",
            )
        ):
            return "ask"
        # Question phrases — route to the no-tool direct-answer path
        if any(p in t for p in ("how", "why", "是什么", "怎么办", "怎么")):
            return "ask"
        # Edit triggers — single-step actions get the fast edit path instead
        # of falling into the 200-step ReAct "agent" loop.
        if any(k in t for k in ("install", "fix", "rename", "renam")):
            return "edit"
        if any(k in t for k in ("run test", "run-test", "run_tests", "run the test", "运行测试")):
            return "edit"
        # Default safe: agent (will run sub-agents, can do anything)
        return "agent"

    def _parse_response(self, text: str) -> str:
        """Parse LLM's JSON output into an intent name.

        Falls back to scanning the text for a known intent name if
        JSON parsing fails. Returns "agent" on total failure.
        """
        data = self._safe_json_loads(text) or {}
        if isinstance(data, dict):
            intent = (data.get("intent") or "").lower().strip()
            if intent in self.INTENT_SCHEMA:
                return intent
        # Last-resort: scan text for any known intent name
        text_lower = (text or "").lower()
        for name in _BUILTIN_INTENT_NAMES:
            if name in text_lower:
                return name
        return "agent"

    # ── Public API (backward compatible) ────────────────────────

    async def classify(self, task: str) -> str:
        """Classify a user task into one of ask / edit / agent.

        Backward-compatible wrapper around `extract()` (from base class).
        """
        return await self.extract(task)


# ── Intent router ───────────────────────────────────────────────

# Handler signature: async def handler(task: str) -> str
Handler = Callable[[str], Awaitable[str]]


class IntentRouter:
    """Maps intents to handlers. Extensible via register()."""

    def __init__(self):
        self._routes: dict[str, Handler] = {}
        self._classifier: Optional[IntentClassifier] = None
        self._last_intent: Optional[str] = None  # most recent classification

    def set_classifier(self, classifier: IntentClassifier):
        self._classifier = classifier

    def register(self, intent: str, handler: Handler):
        """Register a handler for an intent. Use to add new task types."""
        self._routes[intent] = handler

    def get(self, intent: str) -> Optional[Handler]:
        return self._routes.get(intent)

    def cache_stats(self) -> Optional[dict]:
        """Return cache stats from classifier, if available.

        PR-17: delegates to the LLMExtractor's `cache_stats()` method
        (which returns the same {size, live, ttl} dict shape that
        `_IntentCache.stats()` used to return).
        """
        if self._classifier is None:
            return None
        return self._classifier.cache_stats()

    async def route(self, task: str, dispatch_task: Optional[str] = None) -> str:
        """Classify the task and dispatch to the matching handler.

        Args:
            task: The RAW user task — this is what the classifier sees, so it
                is NOT biased by file-context prefixes.
            dispatch_task: Optional alternate payload handed to the handler.
                Used by the CLI to pass the ``[Files: ...]``-prefixed task to
                the handler while classifying the raw task. Defaults to
                ``task`` when omitted (single-task CLI path).
        """
        if not self._classifier:
            intent = "agent"
        else:
            intent = await self._classifier.classify(task)
        self._last_intent = intent

        handler = self._routes.get(intent)
        if handler is None:
            handler = self._routes.get("agent")
        if handler is None:
            return f"No handler for intent '{intent}'"

        return await handler(dispatch_task if dispatch_task is not None else task)
