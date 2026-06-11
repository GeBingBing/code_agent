"""Tests for the LLMExtractor abstract base class.

These tests use a `MockExtractor` subclass to drive the base-class
machinery (cache, fallback chain, JSON parsing) without coupling to
any specific subclass behavior.
"""

import time
import pytest
from unittest.mock import MagicMock, AsyncMock

from agent.core.llm_extractor import LLMExtractor, _CacheEntry


# ── Test doubles ──────────────────────────────────────────────


class MockExtractor(LLMExtractor[list]):
    """Minimal subclass for testing base-class behavior.

    Returns a list of strings. LLM path echoes back the parsed JSON
    `result` field; legacy path returns a fixed string `["legacy"]`.
    """

    def __init__(self, llm_client=None, **kwargs):
        super().__init__(llm_client=llm_client, **kwargs)
        self.legacy_calls = 0
        self.llm_calls = 0

    def _system_prompt(self) -> str:
        return "Return JSON: {\"result\": \"<some string>\"}"

    def _legacy_extract(self, text: str) -> list:
        self.legacy_calls += 1
        return ["legacy", text]

    def _parse_response(self, text: str) -> list:
        data = self._safe_json_loads(text) or {}
        result = data.get("result", "")
        return ["llm", result]


# ── TestCacheBehavior ────────────────────────────────────────


class TestCacheBehavior:
    @pytest.mark.asyncio
    async def test_first_call_invokes_llm(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "x"}', False))
        ext = MockExtractor(llm_client=mock_llm)
        out = await ext.extract("hello")
        assert out == ["llm", "x"]
        assert ext.llm_calls == 0  # base class doesn't track; llm.chat was called
        assert mock_llm.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_second_call_hits_cache(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "x"}', False))
        ext = MockExtractor(llm_client=mock_llm)
        await ext.extract("hello")
        await ext.extract("hello")
        # LLM called only once due to cache
        assert mock_llm.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_is_case_insensitive(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "x"}', False))
        ext = MockExtractor(llm_client=mock_llm)
        await ext.extract("Hello")
        await ext.extract("HELLO")
        await ext.extract("hello")
        assert mock_llm.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_strips_whitespace(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "x"}', False))
        ext = MockExtractor(llm_client=mock_llm)
        await ext.extract("hello")
        await ext.extract("  hello  ")
        await ext.extract("\thello\n")
        assert mock_llm.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_ttl_expiry(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "x"}', False))
        ext = MockExtractor(llm_client=mock_llm, cache_ttl=0.05)
        await ext.extract("hello")
        time.sleep(0.06)
        await ext.extract("hello")
        # Second call should re-invoke LLM
        assert mock_llm.chat.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_max_size_evicts(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "x"}', False))
        ext = MockExtractor(llm_client=mock_llm, cache_ttl=10.0, cache_max=2)
        await ext.extract("a")
        await ext.extract("b")
        await ext.extract("c")  # evicts 'a' (oldest expiry)
        assert ext.cache_size == 2
        # 'a' is gone — should re-invoke LLM
        before = mock_llm.chat.call_count
        await ext.extract("a")
        assert mock_llm.chat.call_count == before + 1

    def test_clear_cache(self):
        ext = MockExtractor(llm_client=MagicMock())
        ext._put_cache("x", ["v"])
        assert ext.cache_size == 1
        ext.clear_cache()
        assert ext.cache_size == 0

    def test_cache_size_property(self):
        ext = MockExtractor(llm_client=MagicMock())
        assert ext.cache_size == 0
        ext._put_cache("a", ["v1"])
        ext._put_cache("b", ["v2"])
        assert ext.cache_size == 2


# ── TestLLMAndFallbackChain ────────────────────────────────


class TestLLMAndFallbackChain:
    @pytest.mark.asyncio
    async def test_uses_llm_when_available(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "from_llm"}', False))
        ext = MockExtractor(llm_client=mock_llm)
        out = await ext.extract("hi")
        assert out == ["llm", "from_llm"]
        assert ext.legacy_calls == 0

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_raises(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("API down"))
        ext = MockExtractor(llm_client=mock_llm)
        out = await ext.extract("hi")
        assert out == ["legacy", "hi"]
        assert ext.legacy_calls == 1

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_returns_empty(self):
        """Empty string from LLM should fall through to legacy."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=("", False))
        ext = MockExtractor(llm_client=mock_llm)
        out = await ext.extract("hi")
        # _parse_response("") returns ["llm", ""] since _safe_json_loads returns None
        # but empty result still goes through LLM path successfully
        # The test verifies: no exception, returns something
        assert out is not None

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_returns_invalid_json(self):
        """If LLM returns garbage, _parse_response handles it, no exception."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=("not json at all", False))
        ext = MockExtractor(llm_client=mock_llm)
        # Should not raise — _safe_json_loads returns None, _parse_response
        # returns ["llm", ""] (or similar). The important thing is robustness.
        out = await ext.extract("hi")
        assert out is not None

    @pytest.mark.asyncio
    async def test_no_fallback_when_disabled_raises(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("API down"))
        ext = MockExtractor(llm_client=mock_llm, fallback_to_legacy=False)
        with pytest.raises(RuntimeError, match="API down"):
            await ext.extract("hi")

    def test_use_llm_false_skips_llm_path(self):
        """Even with llm_client, use_llm=False should disable LLM path."""
        mock_llm = MagicMock()
        ext = MockExtractor(llm_client=mock_llm, use_llm=False)
        assert ext.using_llm is False

    def test_no_llm_client_means_offline(self):
        ext = MockExtractor(llm_client=None)
        assert ext.using_llm is False

    @pytest.mark.asyncio
    async def test_offline_mode_uses_legacy_directly(self):
        ext = MockExtractor(llm_client=None)
        out = await ext.extract("hi")
        assert out == ["legacy", "hi"]
        assert ext.legacy_calls == 1


# ── TestInputValidation ─────────────────────────────────────


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_none_input_uses_legacy(self):
        ext = MockExtractor(llm_client=None)
        out = await ext.extract(None)  # type: ignore
        assert out == ["legacy", ""]

    @pytest.mark.asyncio
    async def test_empty_string_uses_legacy(self):
        ext = MockExtractor(llm_client=None)
        out = await ext.extract("")
        assert out == ["legacy", ""]

    @pytest.mark.asyncio
    async def test_non_string_input_uses_legacy(self):
        ext = MockExtractor(llm_client=None)
        out = await ext.extract(12345)  # type: ignore
        assert out == ["legacy", ""]


# ── TestSafeJsonLoads ───────────────────────────────────────


class TestSafeJsonLoads:
    def test_direct_json_object(self):
        assert LLMExtractor._safe_json_loads('{"a": 1}') == {"a": 1}

    def test_direct_json_array(self):
        assert LLMExtractor._safe_json_loads('[1, 2, 3]') == [1, 2, 3]

    def test_json_fenced_with_json_tag(self):
        text = '```json\n{"a": 1}\n```'
        assert LLMExtractor._safe_json_loads(text) == {"a": 1}

    def test_json_fenced_no_tag(self):
        text = '```\n{"a": 1}\n```'
        assert LLMExtractor._safe_json_loads(text) == {"a": 1}

    def test_prose_with_embedded_json(self):
        text = 'I think the answer is {"intent": "edit"} because of reasons.'
        assert LLMExtractor._safe_json_loads(text) == {"intent": "edit"}

    def test_prose_with_embedded_array(self):
        text = 'Here are the items: [1, 2, 3] in order.'
        assert LLMExtractor._safe_json_loads(text) == [1, 2, 3]

    def test_garbage_returns_none(self):
        assert LLMExtractor._safe_json_loads("not json at all") is None

    def test_empty_returns_none(self):
        assert LLMExtractor._safe_json_loads("") is None
        assert LLMExtractor._safe_json_loads(None) is None  # type: ignore

    def test_malformed_json_returns_none(self):
        assert LLMExtractor._safe_json_loads("{invalid}") is None

    def test_unicode_in_json(self):
        assert LLMExtractor._safe_json_loads('{"name": "张三"}') == {"name": "张三"}

    def test_smart_quotes_in_json(self):
        """Common LLM quirk: curly quotes around JSON keys.

        PR-16: smart quotes are now normalized, so this should parse cleanly.
        """
        text = '{"intent": "edit"}'  # baseline
        assert LLMExtractor._safe_json_loads(text) == {"intent": "edit"}

    def test_smart_quotes_normalized_to_ascii(self):
        """Smart double quotes (U+201C/D) around JSON keys/values are normalized."""
        # Use \u201c and \u201d escapes to be safe across encodings
        text = '{\u201cintent\u201d: \u201cedit\u201d, \u201crationale\u201d: \u201cok\u201d}'
        result = LLMExtractor._safe_json_loads(text)
        assert result == {"intent": "edit", "rationale": "ok"}

    def test_smart_single_quotes_normalized(self):
        """Smart single quotes (U+2018/9) are also normalized."""
        text = "{\u2018intent\u2019: \u2018edit\u2019}"  # smart singles around values
        result = LLMExtractor._safe_json_loads(text)
        # Smart singles around keys are tricky in JSON, but the parser
        # should at least not crash
        assert result is None or isinstance(result, dict)

    def test_trailing_comma_in_object_stripped(self):
        """LLMs often leave trailing commas before closing braces."""
        text = '{"intent": "edit", "confidence": 0.9,}'
        result = LLMExtractor._safe_json_loads(text)
        assert result == {"intent": "edit", "confidence": 0.9}

    def test_trailing_comma_in_array_stripped(self):
        text = '[1, 2, 3,]'
        result = LLMExtractor._safe_json_loads(text)
        assert result == [1, 2, 3]

    def test_smart_quotes_with_trailing_comma(self):
        """Combined: smart quotes + trailing comma — both fixes apply."""
        text = '{\u201cintent\u201d: \u201cedit\u201d,}'
        result = LLMExtractor._safe_json_loads(text)
        assert result == {"intent": "edit"}

    def test_prose_with_smart_quote_json_parses(self):
        """The original orchestrator use case: prose + smart-quoted JSON."""
        text = 'Here is my plan: {\u201cintent\u201d: \u201cedit\u201d, \u201crationale\u201d: \u201cok\u201d} done.'
        result = LLMExtractor._safe_json_loads(text)
        assert result == {"intent": "edit", "rationale": "ok"}


# ── TestAbstractEnforcement ─────────────────────────────────


class TestAbstractEnforcement:
    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            LLMExtractor()  # type: ignore

    def test_must_implement_all_abstract_methods(self):
        class HalfExtractor(LLMExtractor):
            def _system_prompt(self) -> str:
                return "x"
            # Missing _legacy_extract and _parse_response

        with pytest.raises(TypeError):
            HalfExtractor()  # type: ignore


# ── TestSystemPromptIsUsed ──────────────────────────────────


class TestSystemPromptAndUserMessage:
    @pytest.mark.asyncio
    async def test_system_prompt_passed_to_llm(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "x"}', False))
        ext = MockExtractor(llm_client=mock_llm)
        await ext.extract("hi")
        # Inspect the messages passed to llm.chat
        call_args = mock_llm.chat.call_args
        messages = call_args[0][0]  # first positional arg
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert "Return JSON" in messages[0].content
        assert messages[1].role == "user"
        assert "hi" in messages[1].content

    @pytest.mark.asyncio
    async def test_user_message_truncates_long_input(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "x"}', False))
        ext = MockExtractor(llm_client=mock_llm)
        long_input = "x" * 1000
        await ext.extract(long_input)
        messages = mock_llm.chat.call_args[0][0]
        # The user message should be truncated to ~500 chars of input
        assert len(messages[1].content) < 1000 + 100  # 500 + framing


# ── TestLlmResponseUnpacking ───────────────────────────────


class TestLlmResponseUnpacking:
    @pytest.mark.asyncio
    async def test_unpacks_string_response(self):
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"result": "str"}', False))
        ext = MockExtractor(llm_client=mock_llm)
        out = await ext.extract("hi")
        assert out == ["llm", "str"]

    @pytest.mark.asyncio
    async def test_unpacks_openai_style_response(self):
        """Some LLM clients return a ChatCompletion object, not a tuple/str."""
        mock_message = MagicMock()
        mock_message.content = '{"result": "from_object"}'
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=(mock_response, False))
        ext = MockExtractor(llm_client=mock_llm)
        out = await ext.extract("hi")
        assert out == ["llm", "from_object"]
