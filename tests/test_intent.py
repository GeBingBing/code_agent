"""Tests for intent classifier and router.

PR-15 refactor: IntentClassifier now inherits LLMExtractor (LLM-first
with minimal heuristic fallback). The tests in this file are split:

  - Tests marked `@pytest.mark.fallback_mode` (the default for this file)
    exercise the heuristic fallback path. Many old tests (install/fix/
    rename/project-phrases) no longer apply because the LLM now handles
    those — see `TestLLMBasedClassification` for the LLM-path tests.
  - `TestLLMBasedClassification` (no marker) tests the LLM path with
    a mock LLM client.
"""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.core.intent import (
    Intent,
    IntentClassifier,
    IntentRouter,
    INTENT_ASK,
    INTENT_EDIT,
    INTENT_AGENT,
    CLASSIFY_PROMPT,
)


# All tests in this file EXCEPT TestLLMBasedClassification test the
# regex/heuristic fallback path. Apply the marker via the class.
pytestmark = pytest.mark.fallback_mode


# ── TestLegacyHeuristic (formerly TestIntentClassifier) ─────


class TestLegacyHeuristic:
    """Tests for the minimal legacy fallback (no LLM)."""

    def test_legacy_greeting_is_ask(self):
        c = IntentClassifier(llm_client=None)
        assert c._legacy_extract("hi") == "ask"
        assert c._legacy_extract("hello") == "ask"
        assert c._legacy_extract("你好") == "ask"

    def test_legacy_greeting_with_punctuation(self):
        c = IntentClassifier(llm_client=None)
        assert c._legacy_extract("hi.") == "ask"
        assert c._legacy_extract("Hey!") == "ask"

    def test_legacy_self_referential_is_ask(self):
        c = IntentClassifier(llm_client=None)
        assert c._legacy_extract("你是谁") == "ask"
        assert c._legacy_extract("who are you") == "ask"
        assert c._legacy_extract("what can you do") == "ask"

    def test_legacy_install_is_agent_now(self):
        """PR-15: install commands are NO LONGER caught by legacy.
        LLM (Tier 2) now handles these. Legacy defaults to 'agent'."""
        c = IntentClassifier(llm_client=None)
        # install/find/rename no longer hard-coded in legacy
        assert c._legacy_extract("install hermes") == "agent"
        assert c._legacy_extract("fix the bug") == "agent"
        assert c._legacy_extract("rename getCwd to getcwd") == "agent"

    def test_legacy_empty_returns_agent(self):
        c = IntentClassifier(llm_client=None)
        assert c._legacy_extract("") == "agent"
        assert c._legacy_extract(None) == "agent"  # type: ignore

    def test_legacy_ambiguous_returns_agent(self):
        """PR-15: legacy no longer returns None — it always returns a
        string. Ambiguous cases default to 'agent' (safe choice).
        None-returning behavior was PR-14; LLM is now expected to
        resolve ambiguity in the primary path."""
        c = IntentClassifier(llm_client=None)
        assert c._legacy_extract("build a todo app with React") == "agent"
        assert c._legacy_extract("create a REST API for the blog") == "agent"
        assert c._legacy_extract("how does async work") == "agent"


# ── TestIntentRouter ─────────────────────────────────────────


class TestIntentRouter:
    """Test router registration and dispatch."""

    @pytest.mark.asyncio
    async def test_registration_and_dispatch(self):
        router = IntentRouter()
        called = []

        async def handler(task):
            called.append(task)
            return "handled"

        router.register("ask", handler)
        result = await router.get("ask")("hello")
        assert result == "handled"
        assert called == ["hello"]

    @pytest.mark.asyncio
    async def test_unregistered_falls_back_to_agent(self):
        router = IntentRouter()
        called = []

        async def agent_handler(task):
            called.append(task)
            return "agent handled"

        router.register("agent", agent_handler)
        # "ask" is not registered, should fall back to "agent"
        result = await router.get("agent")("some task")
        assert result == "agent handled"

    @pytest.mark.asyncio
    async def test_route_with_classifier(self):
        router = IntentRouter()
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"intent": "edit"}', False))
        router.set_classifier(IntentClassifier(llm_client=mock_llm))

        results = {}
        async def ask_h(task): results["ask"] = task
        async def edit_h(task): results["edit"] = task

        router.register("ask", ask_h)
        router.register("edit", edit_h)
        router.register("agent", lambda t: None)

        await router.route("install hermes")
        assert "edit" in results


# ── TestIntentDataClass ─────────────────────────────────────


class TestIntentDataClass:
    def test_intent_immutable(self):
        i = Intent("test", "desc")
        assert i.name == "test"
        assert i.description == "desc"

    def test_builtin_intents(self):
        assert INTENT_ASK.name == "ask"
        assert INTENT_EDIT.name == "edit"
        assert INTENT_AGENT.name == "agent"


# ── TestClassifyPrompt ──────────────────────────────────────


class TestClassifyPrompt:
    def test_prompt_contains_intents(self):
        prompt = CLASSIFY_PROMPT.format(
            ask_desc=INTENT_ASK.description,
            edit_desc=INTENT_EDIT.description,
            agent_desc=INTENT_AGENT.description,
            task="test task",
        )
        assert "ask" in prompt
        assert "edit" in prompt
        assert "agent" in prompt
        assert "test task" in prompt


# ── TestCacheStats (PR-17) ──────────────────────────────────


class TestCacheStats:
    """Test LLMExtractor's cache_stats() via IntentClassifier.

    PR-17: the legacy `_IntentCache` class is gone. Cache stats now
    come from `LLMExtractor.cache_stats()` (same {size, live, ttl}
    dict shape, kept for callers like /status and IntentRouter).
    """

    @pytest.mark.asyncio
    async def test_cache_stats_after_classify(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"intent": "edit"}', False))
        c = IntentClassifier(llm_client=mock_llm)
        stats_before = c.cache_stats()
        assert stats_before["size"] == 0
        await c.classify("install hermes")
        stats_after = c.cache_stats()
        assert stats_after["size"] == 1
        assert stats_after["live"] == 1
        assert stats_after["ttl"] > 0

    @pytest.mark.asyncio
    async def test_cache_stats_case_insensitive_key(self):
        """Cache key is lowercased — case variations share the slot."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"intent": "edit"}', False))
        c = IntentClassifier(llm_client=mock_llm)
        await c.classify("Install Hermes")
        await c.classify("INSTALL HERMES")
        assert c.cache_stats()["size"] == 1  # both share the lowercased key

    @pytest.mark.asyncio
    async def test_cache_stats_router_delegates(self):
        """IntentRouter.cache_stats() delegates to the classifier."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"intent": "ask"}', False))
        router = IntentRouter()
        router.set_classifier(IntentClassifier(llm_client=mock_llm))
        await router.route("hello there")
        stats = router.cache_stats()
        assert stats is not None
        assert stats["size"] == 1

    def test_router_cache_stats_no_classifier(self):
        """No classifier → None."""
        router = IntentRouter()
        assert router.cache_stats() is None


# ── TestLLMBasedClassification (PR-15) ──────────────────────


@pytest.mark.fallback_mode(False)  # explicitly NOT fallback
class TestLLMBasedClassification:
    """Tests for the LLM-first classification path in PR-15."""

    @pytest.mark.asyncio
    async def test_llm_classifies_install_as_edit(self):
        """Install commands — now classified by LLM, not regex."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            '{"intent": "edit", "confidence": 0.9, "reasoning": "install package"}',
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        result = await c.classify("install hermes")
        assert result == "edit"

    @pytest.mark.asyncio
    async def test_llm_classifies_greeting_as_ask(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            '{"intent": "ask", "confidence": 0.95, "reasoning": "greeting"}',
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        result = await c.classify("hello there")
        assert result == "ask"

    @pytest.mark.asyncio
    async def test_llm_classifies_complex_as_agent(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            '{"intent": "agent", "confidence": 0.85, "reasoning": "multi-step task"}',
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        result = await c.classify("build a user authentication module with JWT")
        assert result == "agent"

    @pytest.mark.asyncio
    async def test_llm_classifies_project_phrase_as_agent(self):
        """PR-14: 'run this project' was a hard-coded project phrase.
        PR-15: handled by LLM (no longer in legacy fallback)."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            '{"intent": "agent", "confidence": 0.9, "reasoning": "needs cwd exploration"}',
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        result = await c.classify("run this project")
        assert result == "agent"

    @pytest.mark.asyncio
    async def test_llm_classifies_chinese_project_phrase(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            '{"intent": "agent", "confidence": 0.9, "reasoning": "启动本项目"}',
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        result = await c.classify("启动本项目")
        assert result == "agent"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_legacy(self):
        """When LLM raises, legacy greeting/self-ref check takes over."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("API down"))
        c = IntentClassifier(llm_client=mock_llm)
        # Greeting is caught by legacy
        result = await c.classify("hello")
        assert result == "ask"
        # Ambiguous case falls through to "agent"
        result = await c.classify("build a todo app")
        assert result == "agent"

    @pytest.mark.asyncio
    async def test_cache_hit_skips_llm(self):
        """Repeated calls hit cache, LLM only called once."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            '{"intent": "edit"}',
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        await c.classify("install numpy")
        await c.classify("install numpy")
        await c.classify("install numpy")
        assert mock_llm.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_offline_mode_no_llm(self):
        """Without LLM, legacy path is used directly."""
        c = IntentClassifier(llm_client=None)
        result = await c.classify("hello")
        assert result == "ask"
        result = await c.classify("build a todo app")
        assert result == "agent"  # default

    @pytest.mark.asyncio
    async def test_json_with_markdown_fence_still_parses(self):
        """LLM that wraps JSON in ```json``` fences is still parsed."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            '```json\n{"intent": "edit"}\n```',
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        result = await c.classify("install foo")
        assert result == "edit"

    @pytest.mark.asyncio
    async def test_json_with_prose_explanation_still_parses(self):
        """LLM that includes prose before/after JSON is still parsed."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            'I think this is edit because: {"intent": "edit"} as the user said.',
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        result = await c.classify("rename x to y")
        assert result == "edit"

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back_to_text_scan(self):
        """When JSON is unparseable, scan text for known intent name."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            "The intent should be 'edit' because...",
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        result = await c.classify("some task")
        assert result == "edit"

    @pytest.mark.asyncio
    async def test_unknown_intent_returns_agent(self):
        """LLM returning an unknown intent name defaults to 'agent'."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(
            '{"intent": "chitchat"}',  # not in our schema
            False,
        ))
        c = IntentClassifier(llm_client=mock_llm)
        result = await c.classify("hello")
        assert result == "agent"

    @pytest.mark.asyncio
    async def test_using_llm_property(self):
        """using_llm property reflects LLM availability."""
        c1 = IntentClassifier(llm_client=None)
        assert c1.using_llm is False
        mock_llm = MagicMock()
        c2 = IntentClassifier(llm_client=mock_llm)
        assert c2.using_llm is True
        c3 = IntentClassifier(llm_client=mock_llm, use_llm=False)
        assert c3.using_llm is False

    @pytest.mark.asyncio
    async def test_system_prompt_describes_intents(self):
        """The system prompt should describe the intent schema to the LLM."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"intent": "ask"}', False))
        c = IntentClassifier(llm_client=mock_llm)
        await c.classify("hi")
        call_args = mock_llm.chat.call_args
        messages = call_args[0][0]
        system_msg = messages[0].content
        # All 3 intents should be in the schema description
        for name in ("ask", "edit", "agent"):
            assert name in system_msg
        # And instructions
        assert "JSON" in system_msg
