"""Base class for LLM-based structured extraction with heuristic fallback.

This module solves the "hard-coded regex list" anti-pattern that both
`agent.core.intent` and `agent.core.fact_extractor` had under PR-14.
Both were enumerating world states as Python literals (PATTERNS lists,
NAME_BLACKLIST, greeting lists, length-5 rule) and broke repeatedly
when alternation order was wrong or a new phrasing appeared.

Design:
  - Subclasses describe WHAT to extract via a JSON schema in a system
    prompt. The LLM does the actual classification.
  - A regex/heuristic `_legacy_extract()` is required as offline fallback
    (mock mode, no API key, network errors).
  - A small in-memory LRU cache (TTL-bounded) makes repeated calls free.
  - A shared `_safe_json_loads()` handles 3 forms of LLM output:
    raw JSON, ```json``` fenced, and prose-with-embedded-JSON.

Tiered pipeline (in `extract()`):
  1. Cache hit (key = lowercased input)
  2. LLM call → JSON parse → domain object
  3. Legacy regex/heuristic fallback

Thread-safety: not thread-safe (caller's responsibility). Same as
`_IntentCache` from PR-14.
"""

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, Optional, TypeVar


T = TypeVar("T")


@dataclass
class _CacheEntry(Generic[T]):
    """One cached extraction result with TTL."""
    result: T
    expires_at: float


class LLMExtractor(ABC, Generic[T]):
    """Tiered structured extractor: cache → LLM → legacy fallback.

    Subclasses MUST implement three methods:
      - `_system_prompt()`: str — the LLM instruction with embedded schema
      - `_legacy_extract(text)`: T — regex/heuristic for offline mode
      - `_parse_response(text)`: T — parse LLM's raw output → domain object

    Public API:
      - `async extract(text) -> T` — main entrypoint
      - `using_llm` (property) — whether LLM is the active path
      - `cache_size` (property) — for tests and /status

    Robustness contract:
      - LLM errors NEVER raise if `fallback_to_legacy=True` (default)
      - Empty / non-string input returns the legacy result on ""
      - Malformed JSON in LLM output falls through to legacy
    """

    DEFAULT_CACHE_TTL = 300.0  # 5 minutes — matches _IntentCache
    DEFAULT_CACHE_MAX = 256

    def __init__(self, llm_client=None, use_llm: bool = True,
                 fallback_to_legacy: bool = True,
                 cache_ttl: float = DEFAULT_CACHE_TTL,
                 cache_max: int = DEFAULT_CACHE_MAX):
        self._llm = llm_client
        self._use_llm = bool(use_llm) and llm_client is not None
        self._fallback = fallback_to_legacy
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_ttl = cache_ttl
        self._cache_max = cache_max

    # ── Public introspection ───────────────────────────────────────

    @property
    def using_llm(self) -> bool:
        """True if LLM path is active (llm_client provided and use_llm=True)."""
        return self._use_llm

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    @property
    def llm_client(self):
        """The underlying LLM client (or None if not provided)."""
        return self._llm

    # ── Main pipeline ──────────────────────────────────────────────

    async def extract(self, text: str) -> T:
        """Tier 1: cache → Tier 2: LLM → Tier 3: legacy fallback.

        Caches the final result (whichever tier produced it) under
        `text.strip().lower()` for `cache_ttl` seconds.
        """
        if not isinstance(text, str):
            return self._legacy_extract("")

        # Tier 1: cache
        key = text.strip().lower()
        if key:
            entry = self._cache.get(key)
            if entry is not None:
                if time.time() < entry.expires_at:
                    return entry.result
                # Expired — drop and fall through
                del self._cache[key]

        # Tier 2: LLM
        if self._use_llm:
            try:
                result = await self._extract_with_llm(text)
                if key:
                    self._put_cache(key, result)
                return result
            except Exception:
                if not self._fallback:
                    raise

        # Tier 3: legacy fallback
        result = self._legacy_extract(text)
        if key:
            self._put_cache(key, result)
        return result

    async def _extract_with_llm(self, text: str) -> T:
        """Call the LLM and parse its response. Subclasses don't override this."""
        # Imported lazily to avoid circular import: llm.client → ... → core
        from ..llm.client import Message
        messages = [
            Message(role="system", content=self._system_prompt()),
            Message(role="user", content=self._user_message(text)),
        ]
        resp, _ = await self._llm.chat(messages, stream=False)
        text_resp = resp if isinstance(resp, str) else getattr(
            resp.choices[0].message, "content", ""
        )
        if not isinstance(text_resp, str):
            text_resp = str(text_resp)
        return self._parse_response(text_resp)

    def _user_message(self, text: str) -> str:
        """Build the user-role message. Subclasses can override for custom framing."""
        return f'User said: "{text[:500]}"\n\nOutput JSON:'

    # ── Cache management ───────────────────────────────────────────

    def _put_cache(self, key: str, result: T):
        if len(self._cache) >= self._cache_max:
            # Evict the entry closest to expiry
            victim = min(self._cache, key=lambda k: self._cache[k].expires_at)
            del self._cache[victim]
        self._cache[key] = _CacheEntry(
            result=result,
            expires_at=time.time() + self._cache_ttl,
        )

    def clear_cache(self):
        self._cache.clear()

    def cache_stats(self) -> dict:
        """Inspect the cache: {size, live, ttl}.

        Same shape as the legacy PR-14 `_IntentCache.stats()` so callers
        that introspect cache state (e.g. `IntentRouter.cache_stats()`,
        tests, `/status` output) don't need to change.
        """
        now = time.time()
        live = sum(1 for e in self._cache.values() if e.expires_at > now)
        return {
            "size": len(self._cache),
            "live": live,
            "ttl": self._cache_ttl,
        }

    # ── Shared JSON parsing utility ────────────────────────────────

    @staticmethod
    def _safe_json_loads(text: str) -> Optional[object]:
        """Tolerant JSON parse for LLM output.

        Tries up to 6 strategies (each on cleaned + original):
          1. Direct `json.loads(text)` — happy path
          2. Direct on text with smart quotes + trailing commas fixed
          3. Strip ```json ... ``` fence, parse directly
          4. Strip fence, parse with smart-quote/trailing-comma fix
          5. Find first {...} or [...] block, parse directly
          6. Find block, parse with smart-quote/trailing-comma fix

        Returns None on total failure — caller decides what to do.

        Normalizations applied (cheap, idempotent on clean text):
          - Smart double quotes (U+201C/D) → "
          - Smart single quotes (U+2018/9) → '
          - Trailing commas before } or ] → removed (LLM artifact)

        Used by LLMExtractor subclasses (FactExtractor, IntentClassifier)
        AND by agent/agents/{evaluator,orchestrator,dual_review}.py
        after PR-16 consolidation.
        """
        if not text or not isinstance(text, str):
            return None

        def _normalize(s: str) -> str:
            """Strip common LLM artifacts that break json.loads."""
            s = s.replace("\u201c", '"').replace("\u201d", '"')  # smart double
            s = s.replace("\u2018", "'").replace("\u2019", "'")  # smart single
            s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing comma
            return s

        # Build the list of candidate strings to try
        candidates: list[str] = [text]

        # Add fence-stripped candidate if a ```json``` block exists
        fence_match = re.search(
            r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```",
            text,
            re.DOTALL,
        )
        if fence_match:
            candidates.append(fence_match.group(1))

        # Add first-{} or first-[] block candidate (greedy, like dual_review)
        block_match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if block_match:
            candidates.append(block_match.group(1))

        # Dedupe (same text might appear from multiple strategies)
        seen = set()
        unique_candidates = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique_candidates.append(c)

        # Try each candidate with and without normalization
        for c in unique_candidates:
            for normalizer in (lambda x: x, _normalize):
                try:
                    return json.loads(normalizer(c))
                except (json.JSONDecodeError, ValueError):
                    continue

        return None

    # ── Subclass extension points ──────────────────────────────────

    @abstractmethod
    def _system_prompt(self) -> str:
        """The LLM system prompt: schema, instructions, output format."""
        raise NotImplementedError

    @abstractmethod
    def _legacy_extract(self, text: str) -> T:
        """Regex/heuristic fallback. MUST be defined and offline-safe."""
        raise NotImplementedError

    @abstractmethod
    def _parse_response(self, text: str) -> T:
        """Parse the LLM's raw text response into a T-typed result."""
        raise NotImplementedError
