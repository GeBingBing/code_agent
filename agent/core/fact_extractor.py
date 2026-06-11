"""Fact extractor — LLM-based with regex fallback for offline mode.

This is the second half of the "session forgetting" root-cause fix.
UserProfile is in place, and this module now uses an LLM to extract
identity / preference statements from user messages, with a bilingual
regex fallback for offline / mock mode.

Architecture (PR-15):
  - Inherits from `LLMExtractor[List[Tuple[str, str]]]`
  - Tier 1: in-memory cache
  - Tier 2: LLM call with JSON schema → facts list
  - Tier 3: regex fallback (preserved verbatim from PR-14)
  - Conservative NAME_BLACKLIST still applied even in LLM path
    (defense-in-depth — LLM might miss "going" / "trying" false positives)

Heuristic-only by design was PR-14; PR-15 moves to LLM-first with
the same regex as offline safety net.
"""

import hashlib
import json
import re
from typing import List, Tuple

from .llm_extractor import LLMExtractor

# ── Regex fallback constants (PR-14) ─────────────────────────


# (regex_pattern, target_key) tuples
# Patterns are tried in order; first match for a given key wins
# unless the same fact appears multiple times (deduplicated by caller).
# Target_key == "_preference" means generic preference (stable-hash key).
PATTERNS: List[Tuple[str, str]] = [
    # ── English: name (broad) ──
    # IMPORTANT: regex alternation is positional — for "I'm known as
    # Charlie" we need to match the WHOLE phrase, not just "I'm" and
    # then capture "known". So "I'm/I am known as" is its own pattern
    # that comes FIRST, then the general "I am X" pattern.
    (
        r"\b(?:i'?m|i am)\s+known\s+as\s+([A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,30})",
        "name",
    ),
    # Longer alternatives first (e.g. "I am called" before "I am")
    (
        r"\b(?:i am called|i go by|my name is|my name'?s|the name is|name'?s|you can call me|call me|this is|i'?m|i am)\s+([A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,30})",
        "name",
    ),
    # ── Chinese: name (broad — multiple phrasings) ──
    (
        r"(?:我(?:的?名字|的名字)?(?:叫|是|为)|我叫|叫我|可以叫我|请叫我|请叫|名字叫|名字是|我是)\s*([A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,30})",
        "name",
    ),
    # ── Pronouns (English) ──
    (
        r"(?:my pronouns are|i use (?:pronouns|he|she|they))\s+(he/him|she/her|they/them|he|she|they)",
        "pronouns",
    ),
    # ── Pronouns (Chinese) ──
    (r"(?:用|代词是|请用)\s*(他|她|它|他们|她们)", "pronouns"),
    # ── Language (English) ──
    (
        r"\b(?:i (?:speak|prefer|use)|please (?:use|speak|reply in))\s+(english|chinese|japanese|french|german|spanish|中文|英文|日文|法语|德语|西班牙语|汉语)",
        "language",
    ),
    # ── Language (Chinese) ──
    (r"(?:说|用|请用|请说|回复用|用.*回复)\s*(中文|英文|日文|英语|汉语|日语)", "language"),
    # ── Timezone (English) ──
    (r"(?:my timezone is|i'?m in(?: the)?|i live in)\s+([A-Za-z][A-Za-z/_]{2,30})", "timezone"),
    # ── Timezone (Chinese) ──
    (r"(?:我在|时区是|时区为|当前时区)\s*([A-Za-z\u4e00-\u9fff/]{1,30})", "timezone"),
    # ── Expertise (English) ──
    # Note: longer alternatives listed first so regex engine doesn't
    # prematurely match "高级" before "高级开发者" etc.
    (
        r"\bi(?:'m| am)\s+(?:an?\s+|the\s+)?(intermediate level|advanced|beginner|intermediate|expert|senior|junior|novice)",
        "expertise",
    ),
    # ── Expertise (Chinese) ──
    # Longest alternatives first.
    (r"我(?:是)?\s*(高级开发者|中级开发者|初学者|新手|中级|高级|专家|小白|老手)", "expertise"),
    # ── Preferences (English) — generic, key generated from value ──
    (
        r"\bi (?:prefer|like|always (?:do|use|write)|need|want)\s+(.{3,80}?)(?:[\.!?]|$)",
        "_preference",
    ),
    (r"\bplease (?:always|never)\s+(.{3,80}?)(?:[\.!?]|$)", "_preference"),
    # ── Preferences (Chinese) — generic, more conservative ──
    # Original pattern was too broad (matched "记住我明天要开会" → pref).
    # Now requires either "请" (polite request) or longer value (≥5 chars)
    # to reduce false positives on "记住我" / "记一下" etc.
    (
        r"(?:请记住|请记一下|我(?:更)?喜欢|我(?:不)?要(?:求)?)\s*(.{2,60}?)(?:[。！？，]|$)",
        "_preference",
    ),
]


# Words to IGNORE even if they match a name pattern
# These are common English/CJK function words/verbs that would produce
# false positives from statements like "I'm going to fix this".
NAME_BLACKLIST = frozenset(
    {
        # English function words / common verbs
        "a",
        "an",
        "the",
        "not",
        "sorry",
        "hello",
        "hi",
        "hey",
        "going",
        "trying",
        "working",
        "looking",
        "thinking",
        "feeling",
        "doing",
        "having",
        "making",
        "getting",
        "wanting",
        "good",
        "bad",
        "fine",
        "ok",
        "okay",
        "sure",
        "ready",
        "tired",
        "busy",
        "free",
        "happy",
        "sad",
        "confused",
        "interested",
        "curious",
        "concerned",
        "worried",
        "excited",
        "from",
        "with",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        # CJK function words / particles
        "很",
        "的",
        "了",
        "在",
        "是",
        "我",
        "你",
        "他",
        "她",
        "它",
        "们",
        "吗",
        "呢",
        "啊",
        "哦",
        "嗯",
        "这",
        "那",
        "哪",
        "什么",
        "怎么",
        "为什么",
        "谁",
        "哪个",
        "得",
        "地",
        "中",
        "上",
        "下",
        "里",
        "外",
        "前",
        "后",
        "左",
        "右",
    }
)

# CJK sentence-final particles that may cling to a name when the user
# writes casually (e.g. "我是hay啊" → "hay啊"). The regex captures them
# because the name pattern is greedy over CJK, but these particles are
# NOT part of the name. Strip them after the regex match.
# NOTE: keep the set small and explicit — only particles that are
# demonstrably NEVER part of a name in any natural Chinese name.
_NAME_TRAILING_PARTICLES = frozenset(
    {
        "啊",
        "呀",
        "哦",
        "呢",
        "吧",
        "哈",
        "嘿",
        "哼",
        "嗯",
        "嘞",
        "咯",
        "哇",
        "嘛",
        "诶",
        "哟",
        "呐",  # casual sentence closers
    }
)

# Punctuation that the post-processing step in `_regex_extract` also strips
_NAME_TRAILING_PUNCT = ".,!?;:'\"，。！？；："

# Minimum length for a name candidate
MIN_NAME_LENGTH = 1
# Maximum length for a name candidate (after first token)
MAX_NAME_LENGTH = 30

# Maximum input length to scan (anti-DoS)
MAX_INPUT_LENGTH = 2000

# Maximum value length for preferences
MAX_PREF_LENGTH = 200


# ── L0: Question-form guard ──────────────────────────────────
# Heuristic fast-fail: questions are never identity statements.
# "我是谁你知道吗" / "Who am I?" should not be extracted as name=...

# Question signal 1: trailing `?` / `？`. Unambiguous.
# `吗` is intentionally NOT here — it can end a rhetorical follow-up
# clause after a real statement ("我是hay啊 你忘了吗" = "I'm hay,
# did you forget?"). The L1 prompt rule and start-word check
# (`_QUESTION_START_WORDS`) cover the cases where `吗` does signal
# a question, without false-failing on rhetorical clauses.
_QUESTION_END_MARKERS = ("?", "？")

_QUESTION_START_WORDS = (
    # CJK interrogatives (most common first)
    "谁",
    "什么",
    "怎么",
    "为什么",
    "为啥",
    "如何",
    "哪",
    "哪里",
    "哪个",
    "哪些",
    "是不是",
    "对不对",
    "对吗",
    "是吗",
    "行不行",
    "可以吗",
    "能不能",
    "会不会",
    "要不要",
    # English interrogatives
    "who",
    "what",
    "where",
    "when",
    "why",
    "how",
    "which",
    "is",
    "are",
    "do",
    "does",
    "did",
    "can",
    "could",
    "would",
    "should",
    "will",
    "won't",
    "aren't",
    "isn't",
    "don't",
    "doesn't",
    "didn't",
    "can't",
    "couldn't",
)


def _is_question_form(text: str) -> bool:
    """Detect if a user message is a question, not a statement.

    Heuristic fast-fail for fact extraction: questions cannot be identity
    statements. Three signals, any one triggers a True:

      1. The last non-whitespace char is `?` / `？`. Unambiguous.
         Catches "你是谁？", "Who am I?".
      2. The message starts with a question word (谁/什么/怎么/.../
         who/what/where/.../do/does/is/are/...) — either alone, followed
         by a space, or as a CJK prefix of the first token. Catches
         "什么是 hay", "Who are you", "How do I use Python?".
      3. A CJK question word (谁/什么/哪/怎么/为什么/如何) appears
         IMMEDIATELY after an identity-introducing pattern (我是/
         我叫/名字是/name's/my name is/I am/I'm). Catches the original
         bug input "我是谁你知道吗" where `谁` is the first thing
         after "我是".

    Conservative on purpose: false positives only block extraction,
    they don't corrupt state. False negatives fall through to the
    existing NAME_BLACKLIST and LLM-path L1 prompt rule.
    """
    if not text:
        return False
    s = text.rstrip()
    if not s:
        return False

    # Signal 1: trailing question markers
    if s[-1] in _QUESTION_END_MARKERS:
        return True

    # Signal 2: question words at the start
    lower = s.lower()
    first_token = lower.split(maxsplit=1)[0] if lower else ""

    for w in _QUESTION_START_WORDS:
        if lower.startswith(w + " "):
            return True
        if lower.startswith(w + "?"):
            return True
        if lower.startswith(w + "？"):
            return True
        if lower == w:
            return True
        # CJK question word as prefix of first token — catches
        # "什么是 hay" where 什么 is followed immediately by 是.
        # For English single-word question words we require standalone,
        # otherwise "is" in "island" would match.
        if w and ord(w[0]) > 0x4E00:  # CJK range
            if first_token.startswith(w):
                return True

    # Signal 3: CJK question word immediately after identity-introducing
    # pattern. This catches the canonical misidentification case where
    # the user says "我是X" but X is actually a question word like 谁.
    cjk_qwords = ("谁", "什么", "哪", "怎么", "为什么", "如何", "哪个", "哪些", "哪儿")
    # Patterns that introduce identity; if the FIRST character(s) AFTER
    # the pattern is a CJK question word, the whole sentence is a question.
    identity_intro_patterns = (
        "我是",
        "我叫",
        "名字是",
        "名字叫",
        "我的名字是",
        "我的名字叫",
        "我叫做",
        "我是叫",
    )
    for pat in identity_intro_patterns:
        idx = s.find(pat)
        if idx == -1:
            continue
        after_idx = idx + len(pat)
        rest = s[after_idx:].lstrip()
        if not rest:
            continue
        first_char = rest[0]
        # Check if first char of "name" is a CJK question word
        for qw in cjk_qwords:
            if first_char == qw or rest.startswith(qw):
                return True
        # English identity pattern: "I am" / "I'm" / "my name is" etc.
    en_intro_patterns = (
        "i am ",
        "i'm ",
        "my name is ",
        "my name's ",
        "name is ",
        "name's ",
        "i'm called ",
        "i am called ",
        "call me ",
        "this is ",
    )
    for pat in en_intro_patterns:
        idx = lower.find(pat)
        if idx == -1:
            continue
        after_idx = idx + len(pat)
        rest = lower[after_idx:].lstrip()
        # "I'm X" where X is exactly "who" / "what" / a question word
        first_word = rest.split(maxsplit=1)[0].rstrip("?.,!") if rest else ""
        if first_word in ("who", "what", "where", "when", "why", "how", "which"):
            return True

    return False


# ── Regex fallback (PR-14 implementation, moved verbatim) ────


def _regex_extract(
    text: str, patterns: List[Tuple[str, str]] = None, name_blacklist: frozenset = None
) -> List[Tuple[str, str]]:
    """The original PR-14 regex implementation.

    Extracted as a module-level function so it can be used both as the
    `_legacy_extract()` fallback and in tests that need raw regex
    behavior without LLM involvement.
    """
    if patterns is None:
        patterns = PATTERNS
    if name_blacklist is None:
        name_blacklist = NAME_BLACKLIST

    if not text or not isinstance(text, str):
        return []
    if len(text) > MAX_INPUT_LENGTH:
        return []

    # L0: question-form early-exit. Questions cannot be identity statements.
    # Bug fix: "我是谁你知道吗" was previously extracted as name="谁你知道吗"
    # because the regex `(?:我是)\s*(...)` matched greedily and the question
    # ending `吗` wasn't filtered. Now we fast-fail before any regex runs.
    if _is_question_form(text):
        return []

    facts: List[Tuple[str, str]] = []
    seen: set = set()  # dedupe by (key, value_lower)

    for pattern, key in patterns:
        compiled = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        for m in compiled.finditer(text):
            raw_value = m.group(1).strip()
            if not raw_value:
                continue

            # ── Name field cleanup ──
            if key in ("name", "preferred_name"):
                # Take only the first token (avoid trailing words)
                value = raw_value.split()[0] if raw_value else ""
                # Strip trailing CJK sentence-final particles that the
                # greedy regex may have captured. "我是hay啊" → "hay啊" →
                # "hay" (the 啊 is a casual sentence closer, not part of
                # the name). Strip in a loop in case multiple particles
                # cling (e.g. "hay啊哈").
                while value and value[-1] in _NAME_TRAILING_PARTICLES:
                    value = value[:-1]
                value = value.rstrip(_NAME_TRAILING_PUNCT)
                if (
                    not value
                    or len(value) < MIN_NAME_LENGTH
                    or len(value) > MAX_NAME_LENGTH
                    or value.lower() in name_blacklist
                ):
                    continue

            # ── Generic preference → stable key ──
            elif key == "_preference":
                if len(raw_value) > MAX_PREF_LENGTH:
                    raw_value = raw_value[:MAX_PREF_LENGTH]
                # Stable hash key — same value → same key
                h = hashlib.md5(raw_value.encode("utf-8")).hexdigest()[:8]
                key = f"pref_{h}"
                value = raw_value

            # ── Pronouns (English) ──
            elif key == "pronouns" and re.match(r"^(he|she|they)$", raw_value, re.I):
                # "I use he" → "he/him" mapping
                mapping = {"he": "he/him", "she": "she/her", "they": "they/them"}
                value = mapping.get(raw_value.lower(), raw_value)
            else:
                value = raw_value.strip(".,!?;:\"'()[]{}")

            # Dedup
            dedup_key = (key, value.lower())
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            facts.append((key, value))

    return facts


# ── LLMExtractor subclass (PR-15) ─────────────────────────────


# Key alias map — normalizes LLM output to canonical field names.
_KEY_ALIASES = {
    "user_name": "name",
    "username": "name",
    "nickname": "preferred_name",
    "pref_name": "preferred_name",
    "lang": "language",
    "tz": "timezone",
    "level": "expertise",
    "skill": "expertise",
    "expertise_level": "expertise",
}


class FactExtractor(LLMExtractor[List[Tuple[str, str]]]):
    """LLM-first fact extractor with regex fallback.

    Returns list of (key, value) tuples. Caller decides where to store
    them (UserProfile, MemoryManager, etc.).
    """

    # JSON schema the LLM is asked to fill in
    FACT_SCHEMA: dict = {
        "name": "User's full name (e.g. 'hay', 'Alice', '张三')",
        "preferred_name": "Nickname or how they want to be called (e.g. 'H', '小明')",
        "pronouns": "Pronouns (he/him, she/her, they/them, 他, 她, 它)",
        "language": "Preferred language (english, chinese, japanese, etc.)",
        "timezone": "Timezone or city (Asia/Shanghai, Beijing, UTC)",
        "expertise": "Skill level (beginner, intermediate, expert, senior, 高级开发者)",
        "preferences": "Array of free-form preference strings (style, tools, habits)",
    }

    def _system_prompt(self) -> str:
        schema = json.dumps(self.FACT_SCHEMA, indent=2, ensure_ascii=False)
        return f"""You extract user identity / preference facts from their messages.

Each fact has a "key" (one of the types below) and a "value":
{schema}

OUTPUT: Reply with JSON only, e.g.
  {{"facts": [
    {{"key": "name", "value": "hay", "confidence": 0.95}},
    {{"key": "language", "value": "chinese", "confidence": 0.9}}
  ]}}

RULES:
  - Extract ONLY facts the user EXPLICITLY stated
  - No inference (don't guess from context)
  - "I'm hay" → name=hay
  - "请用中文回复" → language=chinese AND preferences=["use Chinese for replies"]
  - No markdown, no explanation outside JSON.
  - QUESTIONS ARE NEVER FACTS: If the message is a question (ends in
    `?` `？` `吗` `呢` `吧`, or starts with 谁/什么/哪/怎么/为什么/如何/
    who/what/where/when/why/how/do/does/is/are), return {{"facts": []}}
    even if the words LOOK like identity words (我是X, I'm X, my name is X).
  - REJECT implausible names: any extracted "name" value that contains
    a question word (谁/什么/哪/怎么/为什么) or is longer than 20 characters
    is probably NOT a real name — drop it."""

    def _legacy_extract(self, text: str) -> List[Tuple[str, str]]:
        """Regex fallback (PR-14 behavior, preserved verbatim)."""
        return _regex_extract(text)

    def _parse_response(self, text: str) -> List[Tuple[str, str]]:
        """Parse LLM JSON output into (key, value) facts."""
        data = self._safe_json_loads(text) or {}
        if not isinstance(data, dict):
            return []
        raw_facts = data.get("facts", [])
        if not isinstance(raw_facts, list):
            return []

        facts: List[Tuple[str, str]] = []
        seen: set = set()
        for f in raw_facts:
            if not isinstance(f, dict):
                continue
            key = (f.get("key") or "").lower().strip()
            value = (f.get("value") or "").strip()
            if not key or not value:
                continue
            # Normalize known aliases
            key = _KEY_ALIASES.get(key, key)
            # Apply name blacklist defensively
            if key in ("name", "preferred_name") and value.lower() in NAME_BLACKLIST:
                continue
            # Preferences get a stable hash key (preserves PR-14 behavior)
            if key == "preferences":
                if len(value) > MAX_PREF_LENGTH:
                    value = value[:MAX_PREF_LENGTH]
                h = hashlib.md5(value.encode("utf-8")).hexdigest()[:8]
                key = f"pref_{h}"
            # Dedup
            dedup_key = (key, value.lower())
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            facts.append((key, value))
        return facts

    # ── M1: multi-turn history support ────────────────────────────

    # Cap history injected into the LLM prompt. 800 chars ≈ 200 tokens
    # for English / 200 CJK chars, which is small enough to leave room
    # for the system prompt + current text + JSON output schema.
    HISTORY_MAX_CHARS = 800

    def _user_message(self, text: str, history: str = "") -> str:
        """Build the user-role message, optionally with prior conversation.

        M1: the history block is labeled "for context only" so the LLM
        doesn't try to extract facts from previous turns. Only the
        LATEST user message is treated as extractable content.

        The base LLMExtractor._user_message takes only `text`; subclasses
        can add kwargs (Python's late binding makes the extra param
        transparent to callers that don't pass it).
        """
        text_truncated = text[:500]
        if not history:
            return f'User said: "{text_truncated}"\n\nOutput JSON:'

        h = history.strip()
        truncated = False
        if len(h) > self.HISTORY_MAX_CHARS:
            h = h[: self.HISTORY_MAX_CHARS]
            truncated = True
        suffix = "\n... (truncated)" if truncated else ""
        return (
            f"Previous conversation (for context only — do NOT extract "
            f"facts from these lines, only from the LATEST user message):\n"
            f'"""\n{h}{suffix}\n"""\n\n'
            f'User said: "{text_truncated}"\n\nOutput JSON:'
        )

    async def extract_with_history(self, text: str, history: str = "") -> List[Tuple[str, str]]:
        """Async extraction with prior conversation as context (M1).

        Returns the list of (key, value) facts WITHOUT applying to
        the profile. The caller (e.g. `extract_and_apply_async` or
        `FactConfirmExtractor`) is responsible for applying.
        """
        from ..llm.client import Message

        messages = [
            Message(role="system", content=self._system_prompt()),
            Message(role="user", content=self._user_message(text, history=history)),
        ]
        if self._llm is None:
            return self._legacy_extract(text)
        try:
            resp, _ = await self._llm.chat(messages, stream=False)
            text_resp = (
                resp
                if isinstance(resp, str)
                else getattr(getattr(resp, "choices", [None])[0], "message", None)
            )
            text_resp = getattr(text_resp, "content", text_resp)
            if not isinstance(text_resp, str):
                text_resp = str(text_resp)
            return self._parse_response(text_resp)
        except Exception:
            # Any LLM failure — fall back to legacy regex path
            return self._legacy_extract(text)

    # ── Backward-compatible public API ───────────────────────────

    # NOTE: `extract()` is the async method from LLMExtractor. Sync
    # callers (PR-14 engine flow, tests) should use:
    #   - `extract_and_apply(text, profile)` — sync, uses regex path
    #   - `_regex_extract(text)` — module-level function, sync
    #   - `await extract(text)` — async, uses LLM with regex fallback

    def extract_and_apply(self, text: str, profile) -> List[Tuple[str, str]]:
        """Sync extract-then-apply (PR-14 API, preserved).

        Uses the regex path directly so the engine's auto-remember
        flow doesn't need to manage an event loop. To use the LLM
        path, call `await extract_and_apply_async(...)` instead.
        """
        facts = _regex_extract(text)
        for key, value in facts:
            if key.startswith("pref_"):
                profile.remember_preference(key, value)
            else:
                profile.remember_fact(key, value)
        return facts

    async def extract_and_apply_async(self, text: str, profile) -> List[Tuple[str, str]]:
        """Async extract-then-apply (PR-15, LLM-first).

        Uses the LLM path with regex fallback. Use this from async
        code (e.g. engine.run_stream) when you want LLM extraction.
        """
        facts = await self.extract(text)
        for key, value in facts:
            if key.startswith("pref_"):
                profile.remember_preference(key, value)
            else:
                profile.remember_fact(key, value)
        return facts


# ── L3: silent LLM confirmation gate ──────────────────────────────

# Confidence threshold below which LLM-confirmed facts are dropped.
# 0.7 = "moderately sure" — catches the case where the LLM itself
# is uncertain whether the user really stated this fact. Facts that
# the LLM is sure about (≥0.7) pass through; unsure ones (0.0–0.69)
# are silently discarded (no user prompt).
CONFIRM_THRESHOLD = 0.7


class FactConfirmExtractor(FactExtractor):
    """Two-stage extractor: extract, then ask LLM to confirm.

    Stage 1: same as `FactExtractor.extract_with_history` — extract
             candidate facts.
    Stage 2: ask the LLM "is the user really stating <fact>?" Reply
             with `verifications` JSON, each with a `confidence`.
             Facts with confidence < CONFIRM_THRESHOLD are dropped.
    Stage 3: apply the survivors to the profile (L2 validation in
             `remember_fact` is the last line of defense).

    The gate is *silent*: low-confidence drops never reach the user.
    """

    CONFIRM_PROMPT_TEMPLATE = """You previously extracted these facts from a user message.
Verify whether the user EXPLICITLY stated each one in the message below.

User message: "{message}"
Extracted facts: {facts}

Reply with JSON only:
{{"verifications": [
  {{"key": "name", "value": "hay", "confidence": 0.95}}
]}}

RULES:
  - confidence 0.0 = the user definitely did NOT state this / contradicted it
  - confidence 0.5 = ambiguous, the user might have meant this
  - confidence 0.95+ = explicitly stated in clear statement form
  - Questions (ends in ? 吗 呢 谁 什么) → 0.0 for any identity fact
  - If message is empty or whitespace → all 0.0
  - Output one entry per fact, same key+value as input
  - Pure JSON, no markdown, no explanation."""

    def _confirm_prompt(self, message: str, facts: list) -> str:
        facts_json = json.dumps(
            [{"key": k, "value": v} for k, v in facts],
            ensure_ascii=False,
        )
        return self.CONFIRM_PROMPT_TEMPLATE.format(
            message=(message or "")[:500],
            facts=facts_json,
        )

    async def _confirm_with_llm(
        self, message: str, facts: List[Tuple[str, str]]
    ) -> List[Tuple[str, str]]:
        """Returns the (key, value) facts that pass CONFIRM_THRESHOLD.

        On any error (LLM failure, malformed JSON, timeout) returns the
        original list unchanged — falling back to L0+L1+L2 protection
        is always safer than dropping valid facts.
        """
        if not facts:
            return []
        from ..llm.client import Message

        prompt = self._confirm_prompt(message, facts)
        try:
            resp, _ = await self._llm.chat([Message(role="system", content=prompt)], stream=False)
            text = (
                resp
                if isinstance(resp, str)
                else getattr(getattr(resp, "choices", [None])[0], "message", None)
            )
            text = getattr(text, "content", text)
            if not isinstance(text, str):
                text = str(text)
        except Exception:
            # LLM failure — fall back to candidates; L2 will still
            # catch the most pathological values.
            return list(facts)

        data = self._safe_json_loads(text) or {}
        if not isinstance(data, dict):
            return list(facts)
        verifications = data.get("verifications", [])
        if not isinstance(verifications, list):
            return list(facts)

        confirmed: List[Tuple[str, str]] = []
        for v in verifications:
            if not isinstance(v, dict):
                continue
            try:
                conf = float(v.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            if conf >= CONFIRM_THRESHOLD:
                key = (v.get("key") or "").lower().strip()
                val = (v.get("value") or "").strip()
                if key and val:
                    confirmed.append((key, val))
        return confirmed

    async def extract_and_apply_async(
        self, text: str, profile, history: str = ""
    ) -> List[Tuple[str, str]]:
        """Two-stage: extract → LLM-confirm → apply (L3 + M1)."""
        # Stage 1: extract candidates only (M1 history flows through)
        candidates = await self.extract_with_history(text, history=history)

        # Stage 2: LLM confirmation gate
        if candidates and self._llm is not None:
            try:
                confirmed = await self._confirm_with_llm(text, candidates)
            except Exception:
                confirmed = list(candidates)  # fallback
        else:
            confirmed = list(candidates)

        # Stage 3: apply to profile (L2 schema check is the last gate)
        for key, value in confirmed:
            if key.startswith("pref_"):
                profile.remember_preference(key, value)
            else:
                profile.remember_fact(key, value)
        return confirmed
