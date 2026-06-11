"""Tests for the UserProfile class — root-cause fix for session amnesia."""

import json

import pytest

from agent.core.user_profile import (
    _CHANGE_LOG_MAX,
    UserProfile,
    _validate_value,
)

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def tmp_profile_path(tmp_path, monkeypatch):
    """Override the default path to a tmp file."""
    p = tmp_path / "user_profile.json"
    monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(p))
    return p


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Make sure no env override leaks between tests."""
    monkeypatch.delenv("CODING_AGENT_USER_PROFILE", raising=False)


# ── TestCreationAndDefaults ──────────────────────────────────────


class TestCreationAndDefaults:
    def test_empty_profile(self):
        p = UserProfile()
        assert p.name is None
        assert p.is_empty()

    def test_all_fields_default_to_none_or_empty(self):
        p = UserProfile()
        assert p.name is None
        assert p.preferred_name is None
        assert p.pronouns is None
        assert p.language is None
        assert p.timezone is None
        assert p.expertise_level is None
        assert p.important_facts == []
        assert p.preferences == {}
        assert p.custom_instructions == ""

    def test_load_when_no_file(self, tmp_profile_path):
        p = UserProfile.load()
        assert p.is_empty()
        assert p.name is None


# ── TestSaveAndLoad ─────────────────────────────────────────────


class TestSaveAndLoad:
    def test_save_creates_file(self, tmp_profile_path):
        p = UserProfile(name="hay")
        p.save()
        assert tmp_profile_path.exists()

    def test_save_writes_valid_json(self, tmp_profile_path):
        p = UserProfile(name="hay", language="chinese")
        p.save()
        data = json.loads(tmp_profile_path.read_text(encoding="utf-8"))
        assert data["name"] == "hay"
        assert data["language"] == "chinese"

    def test_load_round_trip(self, tmp_profile_path):
        p = UserProfile(
            name="hay",
            preferred_name="H",
            pronouns="he/him",
            language="chinese",
            timezone="Asia/Shanghai",
            expertise_level="expert",
            important_facts=["name is hay", "likes PEP 8"],
            preferences={"editor": "vim"},
            custom_instructions="always use type hints",
        )
        p.save()
        loaded = UserProfile.load()
        assert loaded.name == "hay"
        assert loaded.preferred_name == "H"
        assert loaded.pronouns == "he/him"
        assert loaded.language == "chinese"
        assert loaded.timezone == "Asia/Shanghai"
        assert loaded.expertise_level == "expert"
        assert "name is hay" in loaded.important_facts
        assert loaded.preferences["editor"] == "vim"
        assert loaded.custom_instructions == "always use type hints"

    def test_load_corrupted_file_returns_empty(self, tmp_profile_path):
        tmp_profile_path.write_text("not valid json {{{", encoding="utf-8")
        p = UserProfile.load()
        assert p.is_empty()
        assert p.name is None

    def test_load_filters_unknown_fields(self, tmp_profile_path):
        """Forward-compat: extra keys in JSON should be ignored, not crash."""
        data = {"name": "hay", "future_field": "value", "another": 123}
        tmp_profile_path.write_text(json.dumps(data), encoding="utf-8")
        p = UserProfile.load()
        assert p.name == "hay"
        assert not hasattr(p, "future_field")

    def test_atomic_write_no_leftover_tmp(self, tmp_profile_path):
        """No .tmp file should remain after a successful save."""
        p = UserProfile(name="hay")
        p.save()
        # List all files in the dir; only user_profile.json should exist
        files = list(tmp_profile_path.parent.iterdir())
        names = [f.name for f in files]
        assert "user_profile.json" in names
        # No leftover tmp files
        assert not any(".tmp" in n for n in names)


# ── TestRememberFact ────────────────────────────────────────────


class TestRememberFact:
    def test_set_name(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("name", "hay")
        assert p.name == "hay"

    def test_set_name_with_user_prefix(self, tmp_profile_path):
        """'user.name' alias should map to the same field."""
        p = UserProfile()
        p.remember_fact("user.name", "alice")
        assert p.name == "alice"

    def test_set_nickname_aliases_preferred_name(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("nickname", "H")
        assert p.preferred_name == "H"

    def test_set_pronouns(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("pronouns", "she/her")
        assert p.pronouns == "she/her"

    def test_set_language(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("language", "chinese")
        assert p.language == "chinese"

    def test_set_timezone(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("timezone", "Asia/Shanghai")
        assert p.timezone == "Asia/Shanghai"

    def test_set_expertise(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("expertise", "senior")
        assert p.expertise_level == "senior"

    def test_custom_fact_appended(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("favorite_editor", "vim")
        assert "favorite_editor: vim" in p.important_facts

    def test_duplicate_custom_fact_not_appended_twice(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("favorite_editor", "vim")
        p.remember_fact("favorite_editor", "vim")
        assert p.important_facts.count("favorite_editor: vim") == 1

    def test_update_existing_name(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("name", "old")
        p.remember_fact("name", "new")
        assert p.name == "new"

    def test_empty_value_ignored(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("name", "")
        assert p.name is None  # empty string treated as None

    def test_empty_key_ignored(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("", "hay")
        assert p.important_facts == []


# ── TestRememberPreference ─────────────────────────────────────


class TestRememberPreference:
    def test_set_preference(self, tmp_profile_path):
        p = UserProfile()
        p.remember_preference("code_style", "PEP 8")
        assert p.preferences["code_style"] == "PEP 8"

    def test_preferences_lowercased(self, tmp_profile_path):
        p = UserProfile()
        p.remember_preference("Code_Style", "PEP 8")
        assert p.preferences["code_style"] == "PEP 8"


# ── TestForget ──────────────────────────────────────────────────


class TestForget:
    def test_forget_known_field(self, tmp_profile_path):
        p = UserProfile(name="hay")
        assert p.forget("name") is True
        assert p.name is None

    def test_forget_with_user_prefix(self, tmp_profile_path):
        p = UserProfile(name="hay")
        assert p.forget("user.name") is True
        assert p.name is None

    def test_forget_custom_fact(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("favorite_editor", "vim")
        assert p.forget("favorite_editor") is True
        assert p.important_facts == []

    def test_forget_preference(self, tmp_profile_path):
        p = UserProfile()
        p.remember_preference("code_style", "PEP 8")
        assert p.forget("code_style") is True
        assert "code_style" not in p.preferences

    def test_forget_nonexistent_returns_false(self, tmp_profile_path):
        p = UserProfile(name="hay")
        assert p.forget("nonexistent_key") is False


# ── TestToPrompt ────────────────────────────────────────────────


class TestToPrompt:
    def test_empty_returns_empty_string(self):
        p = UserProfile()
        assert p.to_prompt() == ""

    def test_minimal(self, tmp_profile_path):
        p = UserProfile(name="hay")
        text = p.to_prompt()
        assert "<user_profile>" in text
        assert "Name: hay" in text
        assert "</user_profile>" in text

    def test_all_fields(self, tmp_profile_path):
        p = UserProfile(
            name="hay",
            preferred_name="H",
            pronouns="he/him",
            language="chinese",
            timezone="Asia/Shanghai",
            expertise_level="expert",
            important_facts=["likes vim"],
            preferences={"editor": "vim"},
            custom_instructions="use type hints",
        )
        text = p.to_prompt()
        for token in [
            "Name: hay",
            "Preferred name: H",
            "Pronouns: he/him",
            "Language: chinese",
            "Timezone: Asia/Shanghai",
            "Expertise: expert",
            "editor: vim",
            "likes vim",
            "use type hints",
        ]:
            assert token in text, f"Missing {token!r} in: {text}"

    def test_truncates_long_values(self, tmp_profile_path):
        long_val = "x" * 500
        p = UserProfile(preferences={"k": long_val})
        text = p.to_prompt()
        # Long value should be truncated
        assert "..." in text
        assert len(text) < len(long_val) + 200

    def test_facts_capped_to_max_facts(self, tmp_profile_path):
        p = UserProfile()
        for i in range(50):
            p.remember_fact(f"fact_{i}", f"value_{i}")
        text = p.to_prompt(max_facts=10)
        # Should only show the last 10
        assert "fact_49" in text
        assert "fact_0" not in text


# ── TestSummary ─────────────────────────────────────────────────


class TestSummary:
    def test_empty_summary(self):
        p = UserProfile()
        assert "(no profile data yet)" in p.summary()

    def test_summary_includes_fields(self, tmp_profile_path):
        p = UserProfile(name="hay", language="chinese")
        s = p.summary()
        assert "name=hay" in s
        assert "lang=chinese" in s


# ── TestClear ──────────────────────────────────────────────────


class TestClear:
    def test_clear_resets_all(self, tmp_profile_path):
        p = UserProfile(name="hay", language="chinese")
        p.remember_fact("favorite_editor", "vim")
        p.remember_preference("code_style", "PEP 8")
        p.custom_instructions = "test"
        p.clear()
        assert p.is_empty()
        assert p.name is None
        assert p.language is None
        assert p.important_facts == []
        assert p.preferences == {}
        assert p.custom_instructions == ""


# ── TestToDictAndFromDict ──────────────────────────────────────


class TestToDictAndFromDict:
    def test_round_trip(self, tmp_profile_path):
        original = UserProfile(
            name="hay",
            language="chinese",
            important_facts=["a", "b"],
            preferences={"x": "y"},
        )
        d = original.to_dict()
        restored = UserProfile.from_dict(d)
        assert restored.name == "hay"
        assert restored.language == "chinese"
        assert restored.important_facts == ["a", "b"]
        assert restored.preferences == {"x": "y"}

    def test_from_dict_invalid_returns_empty(self):
        p = UserProfile.from_dict("not a dict")
        assert p.is_empty()
        p = UserProfile.from_dict(None)
        assert p.is_empty()


# ── TestValidation (L2) ───────────────────────────────────────────
# Schema check at write time. Catches implausible values that slipped
# past L0 (regex) and L1 (LLM prompt加固).


class TestValidation:
    """L2: _validate_value rejects bad values before they reach disk."""

    def test_name_too_long_rejected(self):
        with pytest.raises(ValueError, match="too long"):
            _validate_value("name", "a" * 21)

    def test_name_with_question_mark_rejected(self):
        with pytest.raises(ValueError, match="question marker"):
            _validate_value("name", "hay?")

    def test_name_with_fullwidth_question_mark_rejected(self):
        with pytest.raises(ValueError, match="question marker"):
            _validate_value("name", "hay？")

    def test_name_in_blacklist_rejected(self):
        # "going" is in NAME_BLACKLIST (common verb, not a name)
        with pytest.raises(ValueError, match="blacklist"):
            _validate_value("name", "going")

    def test_name_with_banned_punctuation_rejected(self):
        # "!" / "*" / "#" / "@" are not legitimate in a name
        for ch in ("!", "*", "#", "@"):
            with pytest.raises(ValueError, match="forbidden char"):
                _validate_value("name", f"hay{ch}")

    def test_valid_name_accepted(self):
        # Must not raise for any of these
        for v in ("hay", "小明", "Bob", "alice-2", "user_42", "李四"):
            _validate_value("name", v)

    def test_valid_chinese_name_accepted(self):
        _validate_value("name", "张三")
        _validate_value("name", "李雷")

    def test_language_too_long_rejected(self):
        with pytest.raises(ValueError, match="too long"):
            _validate_value("language", "a" * 25)

    def test_pronouns_too_long_rejected(self):
        with pytest.raises(ValueError, match="too long"):
            _validate_value("pronouns", "a" * 20)

    def test_empty_value_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_value("name", "")
        with pytest.raises(ValueError, match="empty"):
            _validate_value("name", "   ")

    def test_remember_fact_rejects_bad_name(self, tmp_profile_path):
        """L2 integration: bad name from extractor is dropped, profile unchanged."""
        p = UserProfile(name="hay")  # start with valid name
        p.remember_fact("name", "hay?")  # extractor slipped a "?" through
        assert p.name == "hay", "L2 should have rejected the question-mark name"

    def test_remember_fact_rejects_too_long_value(self, tmp_profile_path):
        p = UserProfile(name="hay")
        p.remember_fact("name", "x" * 25)
        assert p.name == "hay", "L2 should have rejected over-cap name"

    def test_remember_fact_accepts_valid_name(self, tmp_profile_path):
        """Regression: valid name writes must still work after L2 added."""
        p = UserProfile()
        p.remember_fact("name", "小明")
        assert p.name == "小明"

    def test_remember_fact_accepts_chinese_statement(self, tmp_profile_path):
        """Regression: '我是 hay' scenario must still work."""
        p = UserProfile()
        p.remember_fact("name", "hay")
        p.remember_fact("preferred_name", "H")
        p.remember_fact("language", "chinese")
        assert p.name == "hay"
        assert p.preferred_name == "H"
        assert p.language == "chinese"

    def test_remember_fact_accepts_preference(self, tmp_profile_path):
        """Preferences are not name fields; L2 should not interfere."""
        p = UserProfile()
        p.remember_preference("code_style", "PEP 8")
        assert p.preferences.get("code_style") == "PEP 8"


# ── TestChangeLog (L4) ────────────────────────────────────────────
# Per-field audit trail enables /undo profile to revert mistakes.


class TestChangeLog:
    """L4: every mutation appends a ChangeRecord to change_log."""

    def test_remember_fact_appends_to_change_log(self, tmp_profile_path):
        p = UserProfile()
        p.remember_fact("name", "hay")
        assert len(p.change_log) == 1
        rec = p.change_log[0]
        assert rec.action == "remember_fact"
        assert rec.key == "name"
        assert rec.before is None
        assert rec.after == "hay"
        assert rec.source == "extractor"  # default source

    def test_remember_fact_with_source_command(self, tmp_profile_path):
        """Caller (e.g. /remember command) can pass source='command'."""
        p = UserProfile()
        p.remember_fact("name", "alice", source="command")
        assert p.change_log[0].source == "command"

    def test_remember_preference_appends_to_change_log(self, tmp_profile_path):
        p = UserProfile()
        p.remember_preference("code_style", "tabs")
        assert len(p.change_log) == 1
        rec = p.change_log[0]
        assert rec.action == "remember_preference"
        assert rec.key == "code_style"
        assert rec.after == "tabs"

    def test_forget_appends_to_change_log(self, tmp_profile_path):
        p = UserProfile(name="hay")
        # clear the log from initial setattr (we set name= directly, not via remember)
        p.change_log.clear()
        p.forget("name")
        assert len(p.change_log) == 1
        rec = p.change_log[0]
        assert rec.action == "forget"
        assert rec.key == "name"
        assert rec.before == "hay"
        assert rec.after is None

    def test_change_log_persists_across_save_load(self, tmp_profile_path):
        """L4 invariant: change_log must round-trip through disk."""
        p1 = UserProfile()
        p1.remember_fact("name", "hay")
        p1.remember_fact("language", "chinese")
        # Reload from disk
        p2 = UserProfile.load()
        assert len(p2.change_log) == 2
        assert p2.change_log[0].key == "name"
        assert p2.change_log[1].key == "language"

    def test_change_log_capped_at_100(self, tmp_profile_path):
        """Cap at _CHANGE_LOG_MAX to avoid unbounded disk growth."""
        p = UserProfile()
        for i in range(_CHANGE_LOG_MAX + 50):
            p.remember_fact("name", f"name_{i}")
        assert len(p.change_log) == _CHANGE_LOG_MAX
        # Most recent should be the last one we wrote
        assert p.change_log[-1].after == f"name_{_CHANGE_LOG_MAX + 49}"

    def test_remember_fact_with_no_change_does_not_log(self, tmp_profile_path):
        """Re-writing the same value is a no-op and should not bloat the log."""
        p = UserProfile(name="hay")
        p.remember_fact("name", "hay")  # same as current
        assert len(p.change_log) == 0


# ── TestUndoLast (L4) ─────────────────────────────────────────────
# /undo profile uses undo_last() to revert the most recent change.


class TestUndoLast:
    """L4: undo_last() reverts the most recent change."""

    def test_undo_last_reverts_field_value(self, tmp_profile_path):
        p = UserProfile(name="hay")
        p.remember_fact("name", "bob")  # name -> bob, log: [name: hay -> bob]
        reverted = p.undo_last()
        assert p.name == "hay", "undo should restore previous name"
        assert reverted is not None
        assert reverted.key == "name"
        assert reverted.before == "hay"
        assert reverted.after == "bob"
        assert len(p.change_log) == 0  # record popped

    def test_undo_last_reverts_preference(self, tmp_profile_path):
        p = UserProfile()
        p.remember_preference("code_style", "tabs")
        assert p.preferences.get("code_style") == "tabs"
        p.undo_last()
        assert "code_style" not in p.preferences

    def test_undo_last_reverts_forget(self, tmp_profile_path):
        """forget('name') removes the value; undo restores it."""
        p = UserProfile(name="hay")
        p.forget("name")
        assert p.name is None
        p.undo_last()
        assert p.name == "hay", "undo of forget should re-set the field"

    def test_undo_last_reverts_remember_fact_for_important_facts(self, tmp_profile_path):
        """remember_fact of an unknown key appends to important_facts; undo removes."""
        p = UserProfile()
        p.remember_fact("favorite_editor", "vim")
        assert "favorite_editor: vim" in p.important_facts
        p.undo_last()
        assert "favorite_editor: vim" not in p.important_facts

    def test_undo_last_returns_none_when_empty(self, tmp_profile_path):
        p = UserProfile()
        assert p.undo_last() is None

    def test_undo_last_sequential_reverts(self, tmp_profile_path):
        """Multiple undos walk the log in reverse."""
        p = UserProfile()
        p.remember_fact("name", "first")
        p.remember_fact("preferred_name", "second")
        p.remember_fact("language", "third")
        assert len(p.change_log) == 3
        p.undo_last()  # language -> None
        assert p.language is None
        assert p.name == "first"
        p.undo_last()  # preferred_name -> None
        assert p.preferred_name is None
        assert p.name == "first"
        p.undo_last()  # name -> None
        assert p.name is None
        assert p.undo_last() is None  # log empty

    def test_undo_last_saves_after_revert(self, tmp_profile_path):
        """Undo must persist to disk (L4 invariant)."""
        p1 = UserProfile()
        p1.remember_fact("name", "alice")
        p1.undo_last()
        # Reload
        p2 = UserProfile.load()
        assert p2.name is None
        assert len(p2.change_log) == 0
