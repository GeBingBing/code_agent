"""Tests for the FactExtractor — bilingual pattern matcher for user identity.

PR-15 refactor: FactExtractor now inherits LLMExtractor (LLM-first with
regex fallback). Existing tests in this file test the regex fallback path
— they're marked with `@pytest.mark.fallback_mode`. New LLM-based tests
live in `TestLLMBasedExtraction`.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.fact_extractor import (
    CONFIRM_THRESHOLD,
    FactConfirmExtractor,
    FactExtractor,
    _is_question_form,
    _regex_extract,
)
from agent.core.user_profile import UserProfile, _validate_value

# All tests in this file (except TestLLMBasedExtraction) test the regex
# fallback path. They use the module-level _regex_extract function directly
# so they're not coupled to the LLMExtractor base class.
pytestmark = pytest.mark.fallback_mode


@pytest.fixture
def extractor():
    """Returns a function that runs regex extraction (fallback path)."""
    return _regex_extract


# ── TestEnglishNameExtraction ──────────────────────────────────


class TestEnglishNameExtraction:
    def test_im_name(self, extractor):
        facts = extractor("I'm hay")
        assert ("name", "hay") in facts

    def test_i_am_name(self, extractor):
        facts = extractor("I am hay")
        assert ("name", "hay") in facts

    def test_my_name_is(self, extractor):
        facts = extractor("My name is Alice")
        assert ("name", "Alice") in facts

    def test_call_me(self, extractor):
        facts = extractor("Call me Bob")
        assert ("name", "Bob") in facts

    def test_you_can_call_me(self, extractor):
        facts = extractor("You can call me Charlie")
        assert ("name", "Charlie") in facts

    def test_this_is(self, extractor):
        facts = extractor("This is Dave")
        assert ("name", "Dave") in facts

    def test_case_insensitive(self, extractor):
        facts = extractor("I'M HAY")
        assert ("name", "HAY") in facts

    def test_takes_first_token(self, extractor):
        """If user says 'I'm Bob Smith', take only 'Bob'."""
        facts = extractor("I'm Bob Smith")
        assert ("name", "Bob") in facts


# ── TestChineseNameExtraction ──────────────────────────────────


class TestChineseNameExtraction:
    def test_wo_shi(self, extractor):
        facts = extractor("我是小明")
        assert ("name", "小明") in facts

    def test_wo_jiao(self, extractor):
        facts = extractor("我叫张三")
        assert ("name", "张三") in facts

    def test_wo_de_mingzi_shi(self, extractor):
        facts = extractor("我的名字是李四")
        assert ("name", "李四") in facts

    def test_jiao_wo(self, extractor):
        facts = extractor("叫我王五")
        assert ("name", "王五") in facts

    def test_ke_yi_jiao_wo(self, extractor):
        facts = extractor("可以叫我赵六")
        assert ("name", "赵六") in facts

    def test_wo_de_mingzi_jiao(self, extractor):
        """我的名字叫 — the new broader pattern should catch this."""
        facts = extractor("我的名字叫 小明")
        assert ("name", "小明") in facts

    def test_wo_de_mingzi_shi_with_space(self, extractor):
        facts = extractor("我的名字是 王五")
        assert ("name", "王五") in facts

    def test_mingzi_jiao(self, extractor):
        facts = extractor("名字叫 李雷")
        assert ("name", "李雷") in facts

    def test_qing_jiao_wo(self, extractor):
        facts = extractor("请叫我 hanmeimei")
        assert ("name", "hanmeimei") in facts

    def test_english_i_am_called(self, extractor):
        facts = extractor("I am called Bob")
        assert ("name", "Bob") in facts

    def test_english_known_as(self, extractor):
        facts = extractor("I'm known as Charlie")
        assert ("name", "Charlie") in facts

    def test_english_name_with_apostrophe(self, extractor):
        facts = extractor("My name's Alice")
        assert ("name", "Alice") in facts

    def test_english_name_in_chinese_sentence(self, extractor):
        facts = extractor("我是 hay, 来自上海")
        assert ("name", "hay") in facts

    def test_chinese_name_in_english_sentence(self, extractor):
        facts = extractor("Hello, I am 张三")
        # The English pattern catches this
        assert any(name == "张三" for _, name in facts)


# ── TestTrailingParticleStripping ──────────────────────────────
# Regression: previously the regex's greedy CJK match would capture
# sentence-final particles (啊/呀/哦/呢/吧/哈) as part of the name,
# e.g. "我是hay啊" → name="hay啊". These tests pin the fix.


class TestTrailingParticleStripping:
    def test_latin_name_with_a(self, extractor):
        # "我是hay啊" — the 啊 is a casual sentence closer, NOT part of the name
        facts = extractor("我是hay啊")
        assert ("name", "hay") in facts

    def test_latin_name_with_ya(self, extractor):
        facts = extractor("我是hay呀")
        assert ("name", "hay") in facts

    def test_latin_name_with_o(self, extractor):
        facts = extractor("我是hay哦")
        assert ("name", "hay") in facts

    def test_latin_name_with_ne(self, extractor):
        facts = extractor("我是hay呢")
        assert ("name", "hay") in facts

    def test_latin_name_with_ba(self, extractor):
        facts = extractor("我是hay吧")
        assert ("name", "hay") in facts

    def test_chinese_name_with_a(self, extractor):
        facts = extractor("我是小明啊")
        assert ("name", "小明") in facts

    def test_chinese_name_with_ne(self, extractor):
        facts = extractor("我叫张三呢")
        assert ("name", "张三") in facts

    def test_name_in_longer_sentence(self, extractor):
        # The bug from the user's actual session
        facts = extractor("我是hay啊 你忘了吗")
        assert ("name", "hay") in facts

    def test_multiple_particles_stripped(self, extractor):
        # Multiple particles in a row should all be stripped
        facts = extractor("我是hay啊哈")
        assert ("name", "hay") in facts

    def test_punctuation_also_stripped(self, extractor):
        # Trailing punctuation (was already handled) + particle
        facts = extractor("我是hay啊。")
        assert ("name", "hay") in facts

    def test_pure_chinese_name_no_particle_unchanged(self, extractor):
        # Sanity: a real Chinese name with no particle shouldn't change
        facts = extractor("我是王五")
        assert ("name", "王五") in facts


# ── TestPronounsExtraction ─────────────────────────────────────


class TestPronounsExtraction:
    def test_he_him(self, extractor):
        facts = extractor("My pronouns are he/him")
        assert ("pronouns", "he/him") in facts

    def test_she_her(self, extractor):
        facts = extractor("My pronouns are she/her")
        assert ("pronouns", "she/her") in facts

    def test_they_them(self, extractor):
        facts = extractor("My pronouns are they/them")
        assert ("pronouns", "they/them") in facts

    def test_chinese_ta(self, extractor):
        facts = extractor("用他")
        assert ("pronouns", "他") in facts


# ── TestLanguageExtraction ────────────────────────────────────


class TestLanguageExtraction:
    def test_english(self, extractor):
        facts = extractor("I speak english")
        assert ("language", "english") in facts

    def test_chinese(self, extractor):
        facts = extractor("I speak chinese")
        assert ("language", "chinese") in facts

    def test_please_use_chinese(self, extractor):
        facts = extractor("please use chinese")
        assert ("language", "chinese") in facts

    def test_chinese_zhongwen(self, extractor):
        facts = extractor("请用中文回复")
        assert ("language", "中文") in facts


# ── TestTimezoneExtraction ────────────────────────────────────


class TestTimezoneExtraction:
    def test_my_timezone_is(self, extractor):
        facts = extractor("My timezone is Asia/Shanghai")
        assert ("timezone", "Asia/Shanghai") in facts

    def test_i_live_in(self, extractor):
        facts = extractor("I live in Beijing")
        assert ("timezone", "Beijing") in facts

    def test_chinese_wo_zai(self, extractor):
        facts = extractor("我在上海")
        assert ("timezone", "上海") in facts


# ── TestExpertiseExtraction ───────────────────────────────────


class TestExpertiseExtraction:
    def test_beginner(self, extractor):
        facts = extractor("I'm a beginner at Python")
        assert ("expertise", "beginner") in facts

    def test_expert(self, extractor):
        facts = extractor("I'm an expert")
        assert ("expertise", "expert") in facts

    def test_senior(self, extractor):
        facts = extractor("I'm a senior developer")
        assert ("expertise", "senior") in facts

    def test_chinese_chuxuezhe(self, extractor):
        facts = extractor("我是初学者")
        assert ("expertise", "初学者") in facts

    def test_chinese_gaoji(self, extractor):
        facts = extractor("我是高级开发者")
        assert ("expertise", "高级开发者") in facts


# ── TestPreferenceExtraction ──────────────────────────────────


class TestPreferenceExtraction:
    def test_english_prefer(self, extractor):
        facts = extractor("I prefer using type hints")
        # Should be stored with a hash-based key
        prefs = [v for k, v in facts if k.startswith("pref_")]
        assert "using type hints" in prefs

    def test_english_please_always(self, extractor):
        facts = extractor("Please always use double quotes")
        prefs = [v for k, v in facts if k.startswith("pref_")]
        assert "use double quotes" in prefs

    def test_chinese_jizhu(self, extractor):
        facts = extractor("记住我喜欢用 TypeScript")
        prefs = [v for k, v in facts if k.startswith("pref_")]
        assert any("TypeScript" in v for v in prefs)

    def test_chinese_xi_huan(self, extractor):
        facts = extractor("我更喜欢中文回复")
        prefs = [v for k, v in facts if k.startswith("pref_")]
        assert any("中文回复" in v for v in prefs)


# ── TestNameBlacklist ────────────────────────────────────────


class TestNameBlacklist:
    def test_im_going_not_caught(self, extractor):
        """Common English verbs should be excluded from name extraction."""
        facts = extractor("I'm going to fix the bug")
        # 'going' is in blacklist — should not be extracted
        assert ("name", "going") not in facts

    def test_im_trying_not_caught(self, extractor):
        facts = extractor("I'm trying to understand")
        assert ("name", "trying") not in facts

    def test_im_working_not_caught(self, extractor):
        facts = extractor("I'm working on a feature")
        assert ("name", "working") not in facts

    def test_im_sorry_not_caught(self, extractor):
        facts = extractor("I'm sorry, I don't understand")
        assert ("name", "sorry") not in facts


# ── TestEdgeCases ────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_input(self, extractor):
        assert extractor("") == []

    def test_none_input(self, extractor):
        assert extractor(None) == []  # type: ignore

    def test_too_long_input(self, extractor):
        """Inputs over 2000 chars are rejected (anti-DoS)."""
        assert extractor("a" * 3000) == []

    def test_no_match(self, extractor):
        facts = extractor("The weather is nice today")
        assert facts == []

    def test_multiple_facts_in_one_message(self, extractor):
        msg = "I'm hay, I speak english, my timezone is UTC"
        facts = extractor(msg)
        keys = [k for k, _ in facts]
        assert "name" in keys
        assert "language" in keys
        assert "timezone" in keys

    def test_dedupe_same_fact(self, extractor):
        """Same (key, value) should not appear twice."""
        msg = "I'm hay. Also, I'm hay."
        facts = extractor(msg)
        name_hay = [f for f in facts if f == ("name", "hay")]
        assert len(name_hay) == 1


# ── TestQuestionFormGuard (L0) ─────────────────────────────


class TestQuestionFormGuard:
    """L0 bug fix: questions must not be extracted as identity facts.

    Regression for the bug where "我是谁你知道吗" was extracted as
    name="谁你知道吗". The regex pattern `(?:我是)\\s*(...)` matched
    greedily, and the trailing `吗` was not filtered. Now `_regex_extract`
    fast-fails when the input looks like a question.
    """

    def test_question_mark_ending_returns_empty(self, extractor):
        """'你是谁？' ends in `？` — must be classified as a question."""
        facts = extractor("你是谁？")
        assert facts == []

    def test_cjk_question_ending_returns_empty(self, extractor):
        """The original bug input — must NOT extract name='谁你知道吗'."""
        facts = extractor("我是谁你知道吗")
        assert facts == []

    def test_question_start_word_returns_empty(self, extractor):
        """'什么是 hay' starts with question word '什么' — skip extraction."""
        facts = extractor("什么是 hay")
        assert facts == []

    def test_english_question_start_returns_empty(self, extractor):
        """'Who am I?' starts with English question word — skip extraction."""
        facts = extractor("Who am I?")
        assert facts == []

    def test_how_do_i_use_returns_empty(self, extractor):
        """'How do I use Python?' — question, no identity."""
        facts = extractor("How do I use Python?")
        assert facts == []

    def test_ending_吧_is_ambiguous_still_extracts(self, extractor):
        """'你是hay吧' — `吧` is ambiguous (particle OR question).

        `吧` is intentionally NOT in _QUESTION_END_MARKERS because it
        can be either a sentence-final particle on a statement
        ('我是hay吧' — rhetorical closer) or a real question marker
        ('你是hay吧' — asking for confirmation). Letting extraction
        run is safe because the regex's trailing-particle stripping
        removes the `吧` from the captured name. `吗` is the only
        CJK end-marker strong enough to fast-fail.
        """
        facts = extractor("我是hay吧")
        assert ("name", "hay") in facts

    def test_statement_still_extracts(self, extractor):
        """Regression: '我是 hay' must still extract name='hay'."""
        facts = extractor("我是 hay")
        assert ("name", "hay") in facts

    def test_statement_with_period_extracts(self, extractor):
        """'I'm hay.' with period — still a statement, must extract."""
        facts = extractor("I'm hay.")
        assert ("name", "hay") in facts

    def test_preference_statement_extracts(self, extractor):
        """Regression: '请用中文回复' — preference statement, must still work."""
        facts = extractor("请用中文回复")
        # language extraction
        assert any(k == "language" for k, _ in facts)

    def test_name_blacklist_still_works(self, extractor):
        """Existing L0 NAME_BLACKLIST guard still fires for non-questions."""
        facts = extractor("I'm sorry")
        # 'sorry' is in NAME_BLACKLIST — should not extract
        assert not any(v == "sorry" for _, v in facts)

    def test_empty_input_returns_empty(self, extractor):
        """Empty / whitespace input must not crash."""
        assert extractor("") == []
        assert extractor("   ") == []

    def test_is_question_form_helper(self):
        """Direct unit test of the _is_question_form classifier."""
        # Trailing ? / ？ markers (strong signals)
        assert _is_question_form("你是谁？") is True
        assert _is_question_form("Who am I?") is True
        # Signal 3: CJK question word right after 我是
        assert _is_question_form("我是谁") is True
        assert _is_question_form("我是谁你知道吗") is True
        assert _is_question_form("我叫什么") is True
        # Start words
        assert _is_question_form("什么是 hay") is True
        assert _is_question_form("怎么用") is True
        assert _is_question_form("why not") is True
        # English Signal 3: "I am" / "I'm" followed by a question word
        assert _is_question_form("I am who") is True
        assert _is_question_form("I'm what") is True
        # Statements (should NOT be questions)
        assert _is_question_form("我是 hay") is False
        assert _is_question_form("I'm hay.") is False
        assert _is_question_form("请用中文回复") is False
        assert _is_question_form("My name is Alice") is False
        # Ambiguous: `吗` is NOT a fast-fail signal because it can end
        # a rhetorical follow-up clause after a real statement
        # ("我是hay啊 你忘了吗" = "I'm hay, did you forget?").
        # Signal 3 catches the genuine question case ("我是谁").
        assert _is_question_form("我是hay啊 你忘了吗") is False
        # 呢 / 吧 are particles on statements, not fast-fail
        assert _is_question_form("我是hay呢") is False
        assert _is_question_form("我是hay吧") is False
        # Edge cases
        assert _is_question_form("") is False
        assert _is_question_form("   ") is False


# ── TestLLMPromptRules (L1) ────────────────────────────────


class TestLLMPromptRules:
    """L1 bug fix: the LLM extraction prompt must explicitly tell the model
    to NOT extract from question-form input. Without this rule, the LLM
    path (used by `engine.py:1175`) can still mis-extract on questions.
    """

    def _prompt(self):
        # Instantiate FactExtractor without going through the LLM
        # — we just need the system prompt string
        from agent.core.fact_extractor import FactExtractor

        # Bypass __init__ to avoid LLMExtractor setup
        fe = FactExtractor.__new__(FactExtractor)
        return fe._system_prompt()

    def test_prompt_contains_question_rule(self):
        """Prompt must include the 'QUESTIONS ARE NEVER FACTS' rule."""
        prompt = self._prompt()
        assert "QUESTIONS ARE NEVER FACTS" in prompt

    def test_prompt_lists_question_end_markers(self):
        """Prompt must enumerate the CJK + ASCII question markers."""
        prompt = self._prompt()
        assert "吗" in prompt
        assert "呢" in prompt
        assert "吧" in prompt
        assert "?" in prompt
        assert "？" in prompt

    def test_prompt_lists_question_start_words(self):
        """Prompt must enumerate common CJK + English question words."""
        prompt = self._prompt()
        for w in ("谁", "什么", "怎么", "为什么", "who", "what", "why", "how"):
            assert w in prompt, f"Question word {w!r} not in prompt"

    def test_prompt_contains_length_check_rule(self):
        """Prompt must include the 'reject implausible names' rule."""
        prompt = self._prompt()
        assert "20 characters" in prompt
        assert "REJECT" in prompt or "implausible" in prompt.lower()

    def test_prompt_preserves_existing_rules(self):
        """Regression: existing rules must still be present (no removal)."""
        prompt = self._prompt()
        # These were in the original prompt and must still be there
        assert "EXPLICITLY stated" in prompt
        assert "I'm hay" in prompt
        assert "请用中文回复" in prompt
        assert "No markdown" in prompt


# ── TestExtractAndApply ─────────────────────────────────────


class TestExtractAndApply:
    def test_apply_to_profile(self, tmp_path, monkeypatch):
        # Override the default profile path
        profile_file = tmp_path / "user_profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_file))

        profile = UserProfile()
        msg = "I'm hay and I prefer Chinese"
        # Use the class method directly (regex-based, sync).
        ext = FactExtractor()
        applied = ext.extract_and_apply(msg, profile)
        assert profile.name == "hay"
        # Preference should be in preferences dict
        assert any("Chinese" in v for v in profile.preferences.values())

    def test_apply_empty(self, tmp_path, monkeypatch):
        profile_file = tmp_path / "user_profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_file))
        profile = UserProfile()
        ext = FactExtractor()
        applied = ext.extract_and_apply("Hello there", profile)
        assert applied == []
        assert profile.is_empty()


# ── TestCustomPatterns ──────────────────────────────────────


class TestCustomPatterns:
    def test_custom_pattern(self):
        """Patterns can be passed to _regex_extract for testing."""
        from agent.core.fact_extractor import _regex_extract

        custom = [(r"my email is (\S+@\S+)", "email")]
        facts = _regex_extract("my email is test@example.com", patterns=custom)
        assert ("email", "test@example.com") in facts

    def test_custom_blacklist(self):
        """Name blacklist can be extended via _regex_extract."""
        from agent.core.fact_extractor import _regex_extract

        # With hay blacklisted, "I'm hay" should not extract "hay"
        facts = _regex_extract("I'm hay", name_blacklist=frozenset({"hay"}))
        assert ("name", "hay") not in facts


# ── TestLLMBasedExtraction (PR-15) ──────────────────────────


class TestLLMBasedExtraction:
    """Tests for the LLM-first extraction path in PR-15.

    All these tests use a mock LLM. They do NOT have @pytest.mark.fallback_mode.
    """

    @pytest.mark.asyncio
    async def test_llm_extracts_name(self):
        """LLM extracts a name from a natural English sentence."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"facts": [{"key": "name", "value": "Alice"}]}',
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        facts = await ext.extract("My name is Alice, please help me")
        assert ("name", "Alice") in facts

    @pytest.mark.asyncio
    async def test_llm_extracts_multiple_facts(self):
        """LLM extracts multiple facts from one message."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"facts": ['
                '{"key": "name", "value": "Bob"},'
                '{"key": "language", "value": "chinese"},'
                '{"key": "timezone", "value": "Asia/Shanghai"}'
                "]}",
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        facts = await ext.extract("I'm Bob, I speak Chinese, I'm in Shanghai")
        keys = [k for k, _ in facts]
        assert "name" in keys
        assert "language" in keys
        assert "timezone" in keys

    @pytest.mark.asyncio
    async def test_llm_extracts_chinese_name(self):
        """LLM handles Chinese name extraction."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"facts": [{"key": "name", "value": "张三"}]}',
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        facts = await ext.extract("我是张三")
        assert ("name", "张三") in facts

    @pytest.mark.asyncio
    async def test_llm_failure_uses_regex_fallback(self):
        """When LLM raises, regex path takes over."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("API down"))
        ext = FactExtractor(llm_client=mock_llm)
        # "I'm hay" — regex fallback should still catch it
        facts = await ext.extract("I'm hay")
        assert ("name", "hay") in facts

    @pytest.mark.asyncio
    async def test_offline_mode_uses_regex(self):
        """Without an LLM, regex path is used directly."""
        ext = FactExtractor(llm_client=None)
        facts = await ext.extract("I'm Alice, I speak English")
        keys = [k for k, _ in facts]
        assert "name" in keys
        assert "language" in keys

    @pytest.mark.asyncio
    async def test_json_with_markdown_fence_still_parses(self):
        """LLM that wraps JSON in ```json``` fences is still parsed."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '```json\n{"facts": [{"key": "name", "value": "Charlie"}]}\n```',
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        facts = await ext.extract("I'm Charlie")
        assert ("name", "Charlie") in facts

    @pytest.mark.asyncio
    async def test_json_with_prose_explanation_still_parses(self):
        """LLM that includes prose before/after JSON is still parsed."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                'I detected the following: {"facts": [{"key": "name", "value": "Dave"}]} '
                "based on the user message.",
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        facts = await ext.extract("I'm Dave")
        assert ("name", "Dave") in facts

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self):
        """When LLM returns unparseable text, _parse_response returns [].

        This is intentional: we don't fall back to regex on every garbage
        LLM response, because a valid 'no facts' response looks similar
        to garbage. Fallback is for LLM UNAVAILABILITY (network error,
        raise), not for low-quality output. See test_llm_failure_uses_regex_fallback
        for the unavailable case.
        """
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=("not json at all", False))
        ext = FactExtractor(llm_client=mock_llm)
        facts = await ext.extract("I'm hay")
        # LLM "succeeded" (didn't raise) with empty/garbage output → trust it
        assert facts == []

    @pytest.mark.asyncio
    async def test_llm_normalizes_key_aliases(self):
        """LLM might return 'user_name' or 'lang' — we normalize."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"facts": ['
                '{"key": "user_name", "value": "Eve"},'
                '{"key": "lang", "value": "japanese"}'
                "]}",
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        facts = await ext.extract("I'm Eve, I speak Japanese")
        keys = [k for k, _ in facts]
        # Aliases should be normalized
        assert "name" in keys
        assert "language" in keys
        # Original aliases should not appear
        assert "user_name" not in keys
        assert "lang" not in keys

    @pytest.mark.asyncio
    async def test_preferences_get_hash_key(self):
        """'preferences' key gets converted to pref_<hash>."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"facts": [{"key": "preferences", "value": "use tabs"}]}',
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        facts = await ext.extract("I prefer tabs")
        keys = [k for k, _ in facts]
        assert any(k.startswith("pref_") for k in keys)

    @pytest.mark.asyncio
    async def test_name_blacklist_applied_to_llm_output(self):
        """Defense-in-depth: blacklist filters LLM output too."""
        mock_llm = MagicMock()
        # LLM incorrectly extracts 'going' as a name
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"facts": [{"key": "name", "value": "going"}]}',
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        facts = await ext.extract("I'm going to fix the bug")
        assert ("name", "going") not in facts

    @pytest.mark.asyncio
    async def test_cache_repeated_calls(self):
        """Repeated calls hit cache, LLM only called once."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"facts": [{"key": "name", "value": "Frank"}]}',
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        await ext.extract("I'm Frank")
        await ext.extract("I'm Frank")
        await ext.extract("I'm Frank")
        assert mock_llm.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_system_prompt_contains_schema(self):
        """The system prompt should describe the fact schema to the LLM."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value=('{"facts": []}', False))
        ext = FactExtractor(llm_client=mock_llm)
        await ext.extract("hi")
        call_args = mock_llm.chat.call_args
        messages = call_args[0][0]
        system_msg = messages[0].content
        # Schema keys should be mentioned
        for key in ("name", "language", "expertise", "timezone"):
            assert key in system_msg
        # And instructions
        assert "JSON" in system_msg

    @pytest.mark.asyncio
    async def test_extract_and_apply_async_writes_to_profile(self, tmp_path, monkeypatch):
        """Async version should use LLM path and update profile."""
        profile_file = tmp_path / "user_profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_file))
        profile = UserProfile()
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"facts": [{"key": "name", "value": "Grace"}]}',
                False,
            )
        )
        ext = FactExtractor(llm_client=mock_llm)
        applied = await ext.extract_and_apply_async("I'm Grace", profile)
        assert profile.name == "Grace"
        assert ("name", "Grace") in applied


# ── TestValidateValue (L2 from extractor perspective) ──────────────
# Re-validates the L2 schema check on the extractor side. The full
# schema behavior is exhaustively covered in test_user_profile.py;
# these tests focus on the extractor's interaction with validation.


class TestValidateValue:
    """L2: _validate_value is the gate the extractor must pass through."""

    def test_validate_value_module_function(self):
        """Direct unit test of _validate_value."""
        import pytest

        # Valid name
        _validate_value("name", "hay")  # no raise
        # Too long
        with pytest.raises(ValueError):
            _validate_value("name", "x" * 25)
        # Question mark
        with pytest.raises(ValueError):
            _validate_value("name", "hay?")

    def test_extract_applies_validation(self, tmp_path, monkeypatch):
        """L2 integration: remember_fact rejects bad values from extractor.

        Simulates a buggy extractor that slips a question-marked name
        through L0+L1 — L2 in `remember_fact` must catch it.
        """
        profile_path = tmp_path / "user_profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_path))

        profile = UserProfile()
        profile.remember_fact("name", "hay?")  # bad value
        assert profile.name is None, "L2 should have rejected the question-mark name"

        profile.remember_fact("name", "x" * 25)  # too long
        assert profile.name is None, "L2 should have rejected the over-cap name"

        # But valid names work
        profile.remember_fact("name", "hay")
        assert profile.name == "hay"


# ── TestConfirmGate (L3) ──────────────────────────────────────────
# The LLM confirmation gate: low-confidence facts are dropped
# silently before they hit the profile.


class TestConfirmGate:
    """L3: FactConfirmExtractor applies a confidence-gated second LLM call."""

    @pytest.mark.asyncio
    async def test_confirm_low_confidence_drops_fact(self):
        """Mock LLM confirms with confidence 0.3 — fact must be dropped."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            side_effect=[
                # Stage 1: extract returns a name
                ('{"facts": [{"key": "name", "value": "hay"}]}', None),
                # Stage 2: confirm returns low confidence
                ('{"verifications": [{"key": "name", "value": "hay", "confidence": 0.3}]}', None),
            ]
        )
        ext = FactConfirmExtractor(llm_client=mock_llm, fallback_to_legacy=False)
        profile = UserProfile()
        confirmed = await ext.extract_and_apply_async("I think I might be hay", profile)
        assert profile.name is None, "L3 must drop low-confidence fact"
        assert confirmed == []

    @pytest.mark.asyncio
    async def test_confirm_high_confidence_keeps_fact(self):
        """Mock LLM confirms with confidence 0.95 — fact must be applied."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            side_effect=[
                ('{"facts": [{"key": "name", "value": "hay"}]}', None),
                ('{"verifications": [{"key": "name", "value": "hay", "confidence": 0.95}]}', None),
            ]
        )
        ext = FactConfirmExtractor(llm_client=mock_llm, fallback_to_legacy=False)
        profile = UserProfile()
        confirmed = await ext.extract_and_apply_async("我是 hay", profile)
        assert profile.name == "hay"
        assert ("name", "hay") in confirmed

    def test_confirm_threshold_default_is_0_7(self):
        """Module-level CONFIRM_THRESHOLD must be 0.7."""
        assert CONFIRM_THRESHOLD == 0.7

    def test_confirm_prompt_includes_message_and_facts(self):
        """The confirmation prompt must include the original message and facts."""
        ext = FactConfirmExtractor(llm_client=None, fallback_to_legacy=False)
        prompt = ext._confirm_prompt(
            "我是 hay",
            [("name", "hay"), ("language", "chinese")],
        )
        assert "我是 hay" in prompt
        assert "name" in prompt
        assert "hay" in prompt
        assert "language" in prompt
        assert "chinese" in prompt
        assert "verifications" in prompt  # JSON schema key

    @pytest.mark.asyncio
    async def test_confirm_mixed_confidence_drops_only_low(self):
        """Multi-fact: 0.95 passes, 0.5 dropped."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            side_effect=[
                (
                    '{"facts": [{"key": "name", "value": "hay"}, {"key": "language", "value": "chinese"}]}',
                    None,
                ),
                (
                    '{"verifications": ['
                    '{"key": "name", "value": "hay", "confidence": 0.95}, '
                    '{"key": "language", "value": "chinese", "confidence": 0.5}'
                    "]}",
                    None,
                ),
            ]
        )
        ext = FactConfirmExtractor(llm_client=mock_llm, fallback_to_legacy=False)
        profile = UserProfile()
        confirmed = await ext.extract_and_apply_async("我是 hay, 喜欢中文", profile)
        assert profile.name == "hay"
        # language was confirmed at 0.5 — dropped (0.5 < 0.7)
        assert profile.language is None
        assert len(confirmed) == 1
        assert ("name", "hay") in confirmed

    @pytest.mark.asyncio
    async def test_confirm_llm_failure_falls_back_to_candidates(self):
        """If the second LLM call fails, return original candidates.

        Better to risk an L0/L1 false positive than to drop a real fact
        because the confirmation step broke.
        """
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            side_effect=[
                ('{"facts": [{"key": "name", "value": "hay"}]}', None),
                Exception("LLM timeout"),
            ]
        )
        ext = FactConfirmExtractor(llm_client=mock_llm, fallback_to_legacy=False)
        profile = UserProfile()
        confirmed = await ext.extract_and_apply_async("我是 hay", profile)
        assert ("name", "hay") in confirmed  # fallback kept it
        assert profile.name == "hay"


# ── TestMultiTurnHistory (M1) ─────────────────────────────────────
# Injecting prior conversation into the extraction prompt so the LLM
# has context to disambiguate "我是谁" (question) vs "我是X" (statement).


class TestMultiTurnHistory:
    """M1: extract_with_history injects conversation context."""

    def test_user_message_includes_history(self):
        """_user_message(text, history=...) must include both."""
        ext = FactExtractor(llm_client=None)
        out = ext._user_message("我是谁", history="用户: 我是工程师\n助手: 好的")
        assert "我是谁" in out
        assert "我是工程师" in out
        assert "Previous conversation" in out
        assert "context only" in out  # labeling discourages re-extraction

    def test_user_message_truncates_long_history(self):
        """History > HISTORY_MAX_CHARS gets truncated with a marker."""
        ext = FactExtractor(llm_client=None)
        long_history = "x" * 2000
        out = ext._user_message("hi", history=long_history)
        assert "(truncated)" in out
        # The history block should be at most HISTORY_MAX_CHARS + a bit
        # for the truncation marker; well under 2000.
        assert len(out) < 1500  # generous bound including template

    def test_user_message_no_history(self):
        """Empty history → no Previous conversation block."""
        ext = FactExtractor(llm_client=None)
        out = ext._user_message("我是 hay", history="")
        assert "Previous" not in out
        assert "我是 hay" in out

    @pytest.mark.asyncio
    async def test_extract_with_history_calls_llm_with_user_message(self):
        """Mock the LLM; assert the call has the history text in the user message."""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"facts": [{"key": "name", "value": "hay"}]}',
                None,
            )
        )
        ext = FactExtractor(llm_client=mock_llm, fallback_to_legacy=False)
        result = await ext.extract_with_history("我是谁", history="用户: 我是工程师")
        # The LLM was called once with messages containing the history
        assert mock_llm.chat.await_count == 1
        call = mock_llm.chat.await_args
        messages = call[0][0]  # positional arg
        user_msg = messages[1].content
        assert "我是工程师" in user_msg
        assert ("name", "hay") in result
