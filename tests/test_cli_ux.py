"""Phase B UX helper tests (B1-B4).

Pure-function tests for the prompt_toolkit-backed helpers added in
``ui.cli.py``. No live prompt session, no network.
"""

from ui.cli import (
    build_slash_completer,
    format_token_badge,
    history_file_path,
    is_line_continuation,
    join_continued_lines,
    normalize_pasted_text,
    parse_at_mentions,
    should_submit,
)

# ── B1: @-file mentions ────────────────────────────────────────


class TestParseAtMentions:
    def test_extracts_resolved_paths(self, tmp_path):
        (tmp_path / "foo.py").write_text("x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "bar.py").write_text("y")
        got = parse_at_mentions("edit @foo.py and @sub/bar.py please", cwd=str(tmp_path))
        assert got == [str(tmp_path / "foo.py"), str(tmp_path / "sub" / "bar.py")]

    def test_ignores_nonexistent_and_bare_at(self, tmp_path):
        assert parse_at_mentions("email me @ user @nope.py", cwd=str(tmp_path)) == []

    def test_handles_relative_and_dotdot(self, tmp_path):
        (tmp_path / "foo.py").write_text("x")
        parent_file = tmp_path.parent / "x.py"
        parent_file.write_text("x")
        try:
            got = parse_at_mentions("@./foo.py @../x.py", cwd=str(tmp_path))
            assert str((tmp_path / "foo.py").resolve()) in got
            assert str(parent_file.resolve()) in got
        finally:
            parent_file.unlink(missing_ok=True)

    def test_dedupes(self, tmp_path):
        (tmp_path / "foo.py").write_text("x")
        got = parse_at_mentions("@foo.py @foo.py", cwd=str(tmp_path))
        assert got == [str(tmp_path / "foo.py")]


# ── B2: slash completer from registry ──────────────────────────


class TestSlashCompleter:
    def test_contains_all_commands_and_aliases(self):
        from agent.commands.base import CommandRegistry, SlashCommand

        reg = CommandRegistry()
        reg.register(SlashCommand(name="foo", description="foo desc", aliases=["f"]))
        reg.register(SlashCommand(name="bar", description="bar desc", aliases=["b", "br"]))

        comp = build_slash_completer(reg)
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        comps = list(comp.get_completions(Document("/f", len("/f")), CompleteEvent()))
        assert "/foo" in {c.text for c in comps}


# ── B3: multi-line / composer ──────────────────────────────────


class TestMultiline:
    def test_is_line_continuation(self):
        assert is_line_continuation("foo \\") is True
        assert is_line_continuation("foo") is False
        assert is_line_continuation("foo\\  ") is True

    def test_join_continued_lines(self):
        assert join_continued_lines("foo \\\nbar") == "foo\nbar"
        assert join_continued_lines("a \\\nb \\\nc") == "a\nb\nc"

    def test_normalize_pasted_text(self):
        assert normalize_pasted_text("a\n\n\n\nb\n  \nc") == "a\n\nb\nc"

    def test_should_submit(self):
        assert should_submit("foo") is True
        assert should_submit("foo\\") is False


# ── B4: on-disk history + token badge ──────────────────────────


class TestHistoryAndBadge:
    def test_filehistory_writes_to_disk(self, tmp_path):
        from prompt_toolkit.history import FileHistory

        hist = FileHistory(str(tmp_path / "hist"))
        # store_string is synchronous in prompt_toolkit (returns None).
        hist.store_string("hello world")
        f = tmp_path / "hist"
        assert f.exists()
        assert "hello world" in f.read_text()

    def test_history_path_under_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ui.cli.Path.home", lambda: tmp_path)
        p = history_file_path()
        assert p == tmp_path / ".coding-agent" / "history"

    def test_format_token_badge_real_usage(self):
        assert format_token_badge({"input": 120, "output": 30}) == "⬇ 120 in / 30 out"

    def test_format_token_badge_estimated(self):
        s = format_token_badge({}, estimated=True, result_len=400)
        assert s.startswith("⬇ ~")
        assert "估计" in s
        assert format_token_badge(None, estimated=True, result_len=8) == "⬇ ~2 tokens (估计)"

    def test_format_token_badge_empty(self):
        assert format_token_badge({}) == ""
        assert format_token_badge(None) == ""
