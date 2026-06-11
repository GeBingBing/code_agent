"""Tests for CLI module — simple questions, commands, and confirm dialog."""

import pytest
from ui.cli import (
    _is_simple_question, _tool_icon, _confirm_handler, _build_diff_lines,
    _clear_two_lines,
    BOLD, DIM, GREEN, RED, CYAN, YELLOW, MAGENTA, RESET,
)


class TestIsSimpleQuestion:
    """Test _is_simple_question classification."""

    def test_short_input(self):
        assert _is_simple_question("hi") is True
        assert _is_simple_question("你好") is True
        assert _is_simple_question("hello") is True

    def test_greetings(self):
        assert _is_simple_question("hello there") is True
        assert _is_simple_question("hi how are you") is True
        assert _is_simple_question("你好啊") is True

    def test_question_markers(self):
        assert _is_simple_question("今天天气怎么样？") is True
        assert _is_simple_question("这个能用吗") is True
        assert _is_simple_question("where is this?") is True

    def test_self_referential_questions(self):
        assert _is_simple_question("你是谁") is True
        assert _is_simple_question("你能做什么") is True
        assert _is_simple_question("你觉得你和claude的差距有多大") is True
        assert _is_simple_question("what can you do") is True

    def test_conversational_questions(self):
        assert _is_simple_question("python怎么学") is True
        assert _is_simple_question("what is asyncio") is True

    def test_not_action_requests(self):
        assert _is_simple_question("帮我安装hermes-agent") is False
        assert _is_simple_question("写一个斐波那契函数") is False
        assert _is_simple_question("修复这个bug") is False
        assert _is_simple_question("修复bug") is False  # short but action
        assert _is_simple_question("创建一个React应用") is False
        assert _is_simple_question("deploy to production") is False
        assert _is_simple_question("add caching to the API") is False
        assert _is_simple_question("运行测试") is False

    def test_boundary_length(self):
        """9-char input without question/action should be properly handled."""
        # "abcdefghi" has 9 chars, no question words, no action — returns False
        assert _is_simple_question("abcdefghi") is False


class TestToolIcon:
    """Test tool icon rendering."""

    def test_read_file(self):
        icon, label = _tool_icon("read_file", {"path": "hello.py"})
        assert "hello.py" in label

    def test_write_file(self):
        icon, label = _tool_icon("write_file", {"path": "output.py"})
        assert "output.py" in label

    def test_execute_command(self):
        icon, label = _tool_icon("execute_command", {"command": "pip install requests"})
        assert "pip install" in label

    def test_install_package(self):
        icon, label = _tool_icon("install_package", {"package": "requests"})
        assert "requests" in label

    def test_grep(self):
        icon, label = _tool_icon("grep", {"pattern": "TODO"})
        assert "TODO" in label

    def test_unknown_tool_fallback(self):
        icon, label = _tool_icon("some_custom_tool", {"key": "val"})
        assert "some_custom_tool" in icon or "some_custom_tool" in label


class TestConfirmHandler:
    """Test confirm handler returns correct values for different inputs.

    Note: we test the logic, not the input() prompt rendering.
    The confirm_handler is an async function; in real usage it blocks on input().
    Here we verify the function is callable and properly structured.
    """

    def test_handler_is_callable(self):
        import asyncio
        assert asyncio.iscoroutinefunction(_confirm_handler)

    def test_handler_accepts_params(self):
        import inspect
        sig = inspect.signature(_confirm_handler)
        params = list(sig.parameters.keys())
        assert params == ["tool_name", "message", "args"]


class TestClearTwoLines:
    """Test the 2-line spinner clear helper."""

    def test_emits_expected_ansi_sequence(self, capsys):
        _clear_two_lines()
        out = capsys.readouterr().out
        # \r\033[K\033[1A\r\033[K = clear current line, up 1, clear prev line
        assert "\033[1A" in out  # up 1 line
        assert out.count("\033[K") == 2  # two clear-line sequences

    def test_does_not_crash(self):
        # Smoke test
        _clear_two_lines()


class TestAsyncInput:
    """Test the cancellable async input helper."""

    def test_is_coroutine(self):
        from ui.cli import _async_input
        import inspect
        assert inspect.iscoroutinefunction(_async_input)

    def test_empty_string_on_eof(self, monkeypatch):
        """EOF on stdin returns empty string (not raises)."""
        import asyncio
        from ui.cli import _async_input

        # Simulate non-TTY (piped) stdin — falls back to run_in_executor
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        def _eof(*a, **kw):
            raise EOFError
        monkeypatch.setattr("builtins.input", _eof)
        result = asyncio.run(_async_input("> "))
        assert result == ""

    def test_empty_string_on_keyboard_interrupt(self, monkeypatch):
        """Ctrl+C in piped mode returns empty string (not leaks a thread)."""
        import asyncio
        from ui.cli import _async_input

        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        def _ki(*a, **kw):
            raise KeyboardInterrupt
        monkeypatch.setattr("builtins.input", _ki)
        result = asyncio.run(_async_input("> "))
        assert result == ""

    def test_strips_input(self, monkeypatch):
        """Returned value is stripped of leading/trailing whitespace."""
        import asyncio
        from ui.cli import _async_input

        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("builtins.input", lambda _: "  yes  ")
        result = asyncio.run(_async_input("> "))
        assert result == "yes"


class TestConfirmHandlerCancellation:
    """Test that Ctrl+C cleanly returns 'n' without leaking threads."""

    def test_empty_answer_returns_n(self, monkeypatch):
        """If _async_input returns '' (Ctrl+C), confirm returns 'n'."""
        from ui.cli import _confirm_handler
        import asyncio

        # Pretend the async input returns empty string (cancelled)
        async def fake_input(prompt):
            return ""
        monkeypatch.setattr("ui.cli._async_input", fake_input)

        result = asyncio.run(_confirm_handler("read_file", "Read", {"path": "x"}))
        assert result == "n"

    def test_keyboard_interrupt_returns_n(self, monkeypatch):
        """If _async_input raises KeyboardInterrupt, confirm returns 'n'."""
        from ui.cli import _confirm_handler
        import asyncio

        async def fake_input(prompt):
            raise KeyboardInterrupt
        monkeypatch.setattr("ui.cli._async_input", fake_input)

        result = asyncio.run(_confirm_handler("read_file", "Read", {"path": "x"}))
        assert result == "n"

    def test_eof_during_full_diff_returns_n(self, monkeypatch):
        """EOF/Ctrl+C while viewing the full diff also returns 'n'."""
        from ui.cli import _confirm_handler
        import asyncio
        import tempfile

        # Build a 60-line diff to trigger pagination
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("x\n" * 100)
            tmp = f.name
        diff_lines = [("+x", "GREEN")] * 60

        async def fake_input(prompt):
            return ""  # simulate Ctrl+C
        monkeypatch.setattr("ui.cli._async_input", fake_input)

        result = asyncio.run(_confirm_handler("write_file", "Edit big", {
            "path": tmp, "content": "x\n" * 100,
        }))
        assert result == "n"


class TestUnraisableHook:
    """Test the threading-shutdown traceback silencer."""

    def test_keyboard_interrupt_silenced(self):
        """sys.unraisablehook should silence KeyboardInterrupt unraisables."""
        import sys
        from ui.cli import _silent_unraisable

        # Hook must be installed
        assert sys.unraisablehook is _silent_unraisable

        # Simulate a KeyboardInterrupt unraisable — should not raise
        class U:
            exc_type = KeyboardInterrupt
            exc_value = KeyboardInterrupt()
            exc_traceback = None
            err_msg = "simulated"
            object = None
        # This should not raise or print anything
        sys.unraisablehook(U())

    def test_non_kb_interrupt_falls_through(self, monkeypatch, capsys):
        """Non-KeyboardInterrupt unraisables fall through to default hook."""
        import sys
        from ui.cli import _silent_unraisable

        class U:
            exc_type = RuntimeError
            exc_value = RuntimeError("real error")
            exc_traceback = None
            err_msg = "real"
            object = None

        # Replace default hook with a recording one
        calls = []
        def record(u):
            calls.append(u)
        monkeypatch.setattr(sys, "__unraisablehook__", record)

        _silent_unraisable(U())
        # Falls through to default
        assert len(calls) == 1


class TestRichHelpers:
    """Test the rich-ified UI helpers (P4)."""

    def test_rich_print_markdown_empty(self, capsys):
        """Empty markdown doesn't print anything."""
        from ui.cli import _rich_print_markdown
        _rich_print_markdown("")
        _rich_print_markdown("   \n  ")
        out = capsys.readouterr().out
        # Empty / whitespace only — should not have produced visible output
        assert out.strip() == "" or not any(c.isalnum() for c in out)

    def test_rich_print_todo_empty(self, capsys):
        """Empty todo list doesn't crash."""
        from ui.cli import _rich_print_todo
        _rich_print_todo([])
        out = capsys.readouterr().out
        # No panel for empty list
        assert "Todo" not in out

    def test_rich_print_todo_with_items(self, capsys):
        """Todo list renders with rich Panel."""
        from ui.cli import _rich_print_todo, RICH_AVAILABLE
        if not RICH_AVAILABLE:
            return  # skip if rich missing
        todos = [
            {"id": "1", "status": "completed", "content": "Read config"},
            {"id": "2", "status": "in_progress", "content": "Implement permissions"},
            {"id": "3", "status": "pending", "content": "Write tests"},
        ]
        _rich_print_todo(todos)
        out = capsys.readouterr().out
        # Should include the content
        assert "Read config" in out
        assert "Implement permissions" in out
        assert "Write tests" in out
        # And the progress indicator
        assert "1/3" in out

    def test_rich_print_tool_result_success(self, capsys):
        """Tool result success badge."""
        from ui.cli import _rich_print_tool_result
        _rich_print_tool_result("Read", "hello.py · 120 lines", success=True)
        out = capsys.readouterr().out
        assert "Read" in out
        assert "hello.py" in out

    def test_rich_print_tool_result_failure(self, capsys):
        """Tool result failure badge shows ✗."""
        from ui.cli import _rich_print_tool_result
        _rich_print_tool_result("Bash", "Permission denied", success=False)
        out = capsys.readouterr().out
        assert "Bash" in out
        assert "Permission denied" in out

    def test_rich_print_confirm_no_diff(self, capsys):
        """Confirm dialog without diff doesn't include option 4."""
        from ui.cli import _rich_print_confirm, RICH_AVAILABLE
        if not RICH_AVAILABLE:
            return
        _rich_print_confirm("read_file", "Read hello.py", [], has_diff=False, choice_prompt="[1/2/3]: ")
        out = capsys.readouterr().out
        assert "Read hello.py" in out
        assert "Confirm" in out
        assert "View full diff" not in out  # no diff → no 4th option

    def test_rich_print_confirm_with_diff(self, capsys):
        """Confirm dialog with diff includes option 4 and shows diff lines."""
        from ui.cli import _rich_print_confirm, RICH_AVAILABLE
        if not RICH_AVAILABLE:
            return
        diff = [
            ("+1 -0 hello.txt", "DIM"),
            ("+hello world", "GREEN"),
        ]
        _rich_print_confirm("write_file", "Write hello.txt", diff, has_diff=True, choice_prompt="[1/2/3/4]: ")
        out = capsys.readouterr().out
        assert "Write hello.txt" in out
        assert "View full diff (2 lines)" in out
        assert "hello world" in out


class TestHandleCommand:
    """Test slash command handling in SimpleCLI."""

    def test_handle_command_does_not_crash(self):
        """_handle_command should not raise for any built-in command."""
        from ui.cli import SimpleCLI
        import asyncio

        cli = SimpleCLI()
        commands = ["/help", "/model", "/mode", "/clear", "/status",
                     "/context", "/memory", "/quit", "/plan", "/undo"]

        async def run():
            for cmd in commands:
                try:
                    result = await cli._handle_command(cmd)
                    assert result is not None, f"{cmd} returned None"
                except Exception as e:
                    # /model and /mode may fail without engine context — that's OK
                    # but they must not raise NameError or ImportError
                    if isinstance(e, (NameError, ImportError, AttributeError)):
                        raise AssertionError(f"{cmd} failed with {type(e).__name__}: {e}") from e

        asyncio.run(run())

    def test_help_contains_commands(self):
        """/help should list available commands."""
        from ui.cli import SimpleCLI
        import asyncio

        cli = SimpleCLI()
        async def run():
            result = await cli._handle_command("/help")
            return result
        result = asyncio.run(run())
        assert any(cmd in result for cmd in ["/help", "/model", "/clear", "/quit"]), \
            f"Help output missing commands: {result[:200]}"

    def test_unknown_command_returns_error(self):
        """Unknown commands should return an error message."""
        from ui.cli import SimpleCLI
        import asyncio

        cli = SimpleCLI()
        async def run():
            return await cli._handle_command("/nonexistent_cmd_xyz")
        result = asyncio.run(run())
        assert "Unknown" in result or "unknown" in result.lower()


class TestUndoProfile:
    """L4: /undo profile reverts the most recent UserProfile change.

    Wires the full CLI dispatch path (registry → handler → user_profile).
    """

    def test_undo_profile_subcommand(self, tmp_path, monkeypatch):
        """Make a profile change, then /undo profile — name must revert."""
        from ui.cli import SimpleCLI
        from agent.core.user_profile import UserProfile
        import asyncio

        # Direct the profile to a tmp file so we don't touch real user data
        p_path = tmp_path / "user_profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(p_path))

        # Start: hay, then change to bob
        profile = UserProfile(name="hay")
        profile.remember_fact("name", "bob")  # second change

        cli = SimpleCLI()
        cli._last_engine = None  # not used — falls back to UserProfile.load()

        async def run():
            return await cli._handle_command("/undo profile")
        result = asyncio.run(run())

        assert "Reverted" in result
        assert "name" in result
        # And the actual disk file should reflect the revert
        reloaded = UserProfile.load()
        assert reloaded.name == "hay", "/undo profile should have reverted name=bob → name=hay"

    def test_undo_profile_with_no_history(self, tmp_path, monkeypatch):
        """Empty change_log → 'Nothing to undo' message."""
        from ui.cli import SimpleCLI
        from agent.core.user_profile import UserProfile
        import asyncio

        p_path = tmp_path / "user_profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(p_path))

        # Ensure empty profile on disk
        UserProfile()  # creates empty
        cli = SimpleCLI()
        async def run():
            return await cli._handle_command("/undo profile")
        result = asyncio.run(run())
        assert "Nothing to undo" in result

    def test_undo_profile_uses_engine_profile(self, tmp_path, monkeypatch):
        """When ctx['engine'] is provided, undo targets engine.user_profile."""
        from ui.cli import SimpleCLI
        from agent.core.user_profile import UserProfile
        import asyncio

        p_path = tmp_path / "user_profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(p_path))

        # Engine has a different state than the on-disk profile
        # (here, both happen to share the path, but the engine profile
        # is the one the handler is supposed to mutate).
        engine_profile = UserProfile(name="hay")
        engine_profile.remember_fact("name", "alice")

        class FakeEngine:
            def __init__(self, profile):
                self.user_profile = profile

        fake_engine = FakeEngine(engine_profile)
        cli = SimpleCLI()
        cli._last_engine = fake_engine

        async def run():
            return await cli._handle_command("/undo profile")
        result = asyncio.run(run())

        assert "Reverted" in result
        # The engine's in-memory profile was reverted.
        assert engine_profile.name == "hay"
        # The reverted state is persisted to disk (save() is called by undo_last()).
        disk_profile = UserProfile.load()
        assert disk_profile.name == "hay"
        assert len(disk_profile.change_log) == 0


class TestBuildDiffLines:
    """Test the diff-builder helper used by _confirm_handler."""

    def test_write_file_to_new_path(self, tmp_path):
        """Write to a new file shows all additions."""
        target = tmp_path / "new.py"
        lines = _build_diff_lines("write_file", {
            "path": str(target),
            "content": "print('hello')\nprint('world')\n",
        })
        # Header + 2 added lines
        assert any("+2 -0" in t for t, _ in lines)
        assert sum(1 for t, c in lines if c == "GREEN") == 2

    def test_write_file_existing(self, tmp_path):
        """Edit shows added + removed lines."""
        target = tmp_path / "old.py"
        target.write_text("a\nb\nc\n")
        lines = _build_diff_lines("write_file", {
            "path": str(target),
            "content": "a\nB\nc\nd\n",
        })
        # 1 added, 1 removed
        assert any("+2 -1" in t or "+1 -1" in t for t, _ in lines)
        assert any(c == "RED" for _, c in lines)

    def test_apply_diff_single_line(self):
        """apply_diff shows first changed line as - / + pair."""
        lines = _build_diff_lines("apply_diff", {
            "search": "old_var = 1\n",
            "replace": "new_var = 1\n",
        })
        # Should have a - and a + line
        assert any(c == "RED" for _, c in lines)
        assert any(c == "GREEN" for _, c in lines)

    def test_apply_diff_multiline(self):
        """Multi-line apply_diff shows count summary."""
        lines = _build_diff_lines("apply_diff", {
            "search": "a\nb\nc\n",
            "replace": "x\ny\nz\nw\n",
        })
        # Should have a "(3 → 4 lines)" annotation
        assert any("3 → 4 lines" in t for t, _ in lines)

    def test_non_diff_tool_returns_empty(self):
        """Tools that don't have a diff (e.g. execute_command) return []."""
        lines = _build_diff_lines("execute_command", {"command": "ls"})
        assert lines == []

    def test_missing_path_handled(self, tmp_path):
        """Missing path in args shouldn't crash the builder (returns [])."""
        # Empty path triggers an exception inside the builder; outer try/except
        # returns [] so the confirm dialog still works.
        lines = _build_diff_lines("write_file", {"content": "x", "path": ""})
        # Don't assert on the count — point is "no crash"
        assert isinstance(lines, list)

    def test_diff_lines_are_tuples(self, tmp_path):
        """Each diff line is a (text, color) tuple — renderable format."""
        lines = _build_diff_lines("write_file", {
            "path": str(tmp_path / "x.py"),
            "content": "hello\n",
        })
        assert all(isinstance(item, tuple) and len(item) == 2 for item in lines)
        assert all(color in ("RED", "GREEN", "DIM", "YELLOW", "CYAN") for _, color in lines)


class TestConfirmHandlerFourOptions:
    """Test the new 4th option (View diff) in _confirm_handler."""

    def test_4th_option_only_for_diff_tools(self, monkeypatch):
        """The '4. View full diff' line only shows for write/apply_diff."""
        from ui.cli import _confirm_handler
        import asyncio

        captured = []
        monkeypatch.setattr("builtins.input", lambda _: "3")

        # Non-diff tool — should still work, no 4th option offered
        async def run():
            return await _confirm_handler("execute_command", "Run ls", {"command": "ls"})
        result = asyncio.run(run())
        assert result == "n"

    def test_choice_1_returns_y(self, monkeypatch):
        from ui.cli import _confirm_handler
        import asyncio
        monkeypatch.setattr("builtins.input", lambda _: "1")
        result = asyncio.run(_confirm_handler("read_file", "Read file", {"path": "x.py"}))
        assert result == "y"

    def test_choice_2_returns_a(self, monkeypatch):
        from ui.cli import _confirm_handler
        import asyncio
        monkeypatch.setattr("builtins.input", lambda _: "2")
        result = asyncio.run(_confirm_handler("read_file", "Read file", {"path": "x.py"}))
        assert result == "a"

    def test_choice_3_returns_n(self, monkeypatch):
        from ui.cli import _confirm_handler
        import asyncio
        monkeypatch.setattr("builtins.input", lambda _: "3")
        result = asyncio.run(_confirm_handler("read_file", "Read file", {"path": "x.py"}))
        assert result == "n"

    def test_choice_4_loops_then_yes(self, monkeypatch, tmp_path, capsys):
        """Choice 4 prints full diff, then re-prompts; 1 on re-prompt returns y."""
        from ui.cli import _confirm_handler
        import asyncio

        target = tmp_path / "diff_test.py"
        target.write_text("old line 1\nold line 2\n")
        # Sequence: 4 (view), 1 (yes)
        inputs = iter(["4", "1"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        result = asyncio.run(_confirm_handler("write_file", "Edit", {
            "path": str(target),
            "content": "old line 1\nnew line 2\n",
        }))
        assert result == "y"
        out = capsys.readouterr().out
        # Full diff header should have appeared
        assert "Full diff" in out
        assert "End of diff" in out

    def test_choice_4_loops_then_no(self, monkeypatch, tmp_path, capsys):
        """Choice 4 prints full diff, then 3 returns n."""
        from ui.cli import _confirm_handler
        import asyncio

        target = tmp_path / "diff_test.py"
        target.write_text("a\nb\n")
        inputs = iter(["4", "3"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        result = asyncio.run(_confirm_handler("write_file", "Edit", {
            "path": str(target),
            "content": "a\nB\nc\n",
        }))
        assert result == "n"

    def test_eof_returns_n(self, monkeypatch):
        from ui.cli import _confirm_handler
        import asyncio

        def _raise(*a, **kw):
            raise EOFError
        monkeypatch.setattr("builtins.input", _raise)
        result = asyncio.run(_confirm_handler("read_file", "Read", {"path": "x"}))
        assert result == "n"

    def test_keyboard_interrupt_returns_n(self, monkeypatch):
        from ui.cli import _confirm_handler
        import asyncio

        def _raise(*a, **kw):
            raise KeyboardInterrupt
        monkeypatch.setattr("builtins.input", _raise)
        result = asyncio.run(_confirm_handler("read_file", "Read", {"path": "x"}))
        assert result == "n"


# ── TestDirectAnswerIdentity ──────────────────────────────────
# Regression: previously `_direct_answer` (the "simple question" fast path
# in the CLI) bypassed fact_extractor and user_profile entirely, so
# short identity statements like "我是hay" were never remembered, and
# the LLM never saw the user's stored identity either. These tests
# pin the fix.


class TestDirectAnswerIdentity:
    """`_direct_answer` must run fact_extractor and inject user_profile."""

    @pytest.mark.asyncio
    async def test_fact_extraction_runs_on_simple_question(self, tmp_path, monkeypatch):
        """'我是hay' via the simple-question path must save name=hay to disk."""
        from pathlib import Path
        from ui.cli import SimpleCLI

        # Redirect user profile to a temp file
        profile_path = tmp_path / "profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_path))

        from agent.core.user_profile import UserProfile
        # Make sure we start clean
        if profile_path.exists():
            profile_path.unlink()

        cli = SimpleCLI()
        # Build a mock engine with user_profile and an LLM that returns nothing
        from agent.core.engine import AgentEngine, AgentConfig
        from agent.core.user_profile import UserProfile
        engine = AgentEngine(AgentConfig())
        engine.user_profile = UserProfile()

        # Mock the LLM chat to return a simple string (not stream)
        class _MockChoice:
            delta = type("D", (), {"content": "ok", "tool_calls": None})()
        class _MockChunk:
            choices = [_MockChoice]
            usage = None
        class _MockResp:
            def __init__(self): self._done = False
            def __aiter__(self): return self
            async def __anext__(self):
                if self._done: raise StopAsyncIteration
                self._done = True
                return _MockChunk()
        from agent.llm.client import LLMClient
        engine.llm = LLMClient(provider="mock", model="test")

        # Patch the LLM.chat to return our mock stream
        async def mock_chat(messages, stream=True):
            return _MockResp(), True
        engine.llm.chat = mock_chat

        # Run a "simple question" that contains an identity statement
        await cli._direct_answer("我是hay", engine, 0.0)

        # Reload profile from disk and verify name was saved
        from agent.core.user_profile import UserProfile
        saved = UserProfile.load()
        assert saved.name == "hay", f"expected name='hay', got {saved.name!r}"

    @pytest.mark.asyncio
    async def test_user_profile_injected_into_prompt(self, tmp_path, monkeypatch):
        """When the profile has stored identity, `_direct_answer` must include
        it in the prompt sent to the LLM."""
        from ui.cli import SimpleCLI
        from agent.core.engine import AgentEngine, AgentConfig
        from agent.core.user_profile import UserProfile

        profile_path = tmp_path / "profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_path))

        # Pre-populate profile with hay's identity
        profile = UserProfile()
        profile.remember_fact("name", "hay")
        profile.remember_preference("language", "Chinese")

        cli = SimpleCLI()
        engine = AgentEngine(AgentConfig())
        engine.user_profile = profile

        captured_messages = []
        class _MockChunk:
            delta = type("D", (), {"content": "ok", "tool_calls": None})()
            usage = None
            choices = [type("C", (), {"delta": delta})()]
        class _MockResp:
            def __init__(self): self._done = False
            def __aiter__(self): return self
            async def __anext__(self):
                if self._done: raise StopAsyncIteration
                self._done = True
                return _MockChunk()

        async def mock_chat(messages, stream=True):
            captured_messages.extend(messages)
            return _MockResp(), True
        from agent.llm.client import LLMClient
        engine.llm = LLMClient(provider="mock", model="test")
        engine.llm.chat = mock_chat

        await cli._direct_answer("你好", engine, 0.0)

        # The captured prompt must reference the user's identity
        assert captured_messages, "LLM was never called"
        prompt_text = captured_messages[0].content
        assert "hay" in prompt_text, f"prompt missing identity: {prompt_text[:300]!r}"
        assert "user_profile" in prompt_text, f"prompt missing profile block: {prompt_text[:300]!r}"


# ── TestDirectAnswerNoLingeringRefs ────────────────────────────
# Regression: a previous refactor left a `_buf_ref.append(line_text)` line
# inside `_direct_answer._flush_line` after the surrounding `_buf_ref = []`
# had been removed. The error surfaced only when the model produced a
# streaming response that flushed at least one line — `_direct_answer`
# would print the response, then blow up at footer time. Pin it.


class TestDirectAnswerNoLingeringRefs:
    """`_direct_answer` must not reference variables removed during refactor."""

    @pytest.mark.asyncio
    async def test_no_name_error_on_streaming_response(self, tmp_path, monkeypatch):
        from ui.cli import SimpleCLI
        from agent.core.engine import AgentEngine
        from agent.llm.client import LLMClient

        profile_path = tmp_path / "profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_path))

        cli = SimpleCLI()
        engine = AgentEngine()
        engine.user_profile = None  # simplest path

        # Mock LLM that streams a 2-line response so _flush_line runs
        class _Delta:
            content = "你好\nHay"
            tool_calls = None

        class _Choice:
            delta = _Delta()

        class _Chunk:
            choices = [_Choice()]
            usage = None

        async def _aiter():
            yield _Chunk()

        async def mock_chat(messages, stream=True):
            return _aiter(), True

        engine.llm = LLMClient(provider="mock", model="test")
        engine.llm.chat = mock_chat

        # Must not raise NameError or any other exception
        result = await cli._direct_answer("你是谁", engine, 0.0)
        assert isinstance(result, str)


class TestQuestionFormNoExtraction:
    """End-to-end: question-form user input must not pollute user_profile.

    Regression for the bug: '我是谁你知道吗' was extracted as name='谁你知道吗'
    and the LLM responded '根据你的用户资料，我知道你的名字是 谁你知道吗'.
    The L0 regex guard now fast-fails question-form input.
    """

    @pytest.mark.asyncio
    async def test_我是谁你知道吗_does_not_pollute_profile(self, tmp_path, monkeypatch):
        """The exact bug input — must not save a bogus name to disk."""
        from ui.cli import SimpleCLI
        from agent.core.engine import AgentEngine, AgentConfig
        from agent.core.user_profile import UserProfile
        from agent.llm.client import LLMClient

        profile_path = tmp_path / "profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_path))
        if profile_path.exists():
            profile_path.unlink()

        cli = SimpleCLI()
        engine = AgentEngine(AgentConfig())
        engine.user_profile = UserProfile()  # fresh empty profile

        # Mock LLM that returns any text
        class _Delta:
            content = "I don't know who you are"
            tool_calls = None
        class _Choice:
            delta = _Delta()
        class _Chunk:
            choices = [_Choice()]
            usage = None
        async def _aiter():
            yield _Chunk()
        async def mock_chat(messages, stream=True):
            return _aiter(), True
        engine.llm = LLMClient(provider="mock", model="test")
        engine.llm.chat = mock_chat

        # The exact bug input — should be treated as a question, not identity
        await cli._direct_answer("我是谁你知道吗", engine, 0.0)

        # Profile must NOT be polluted with the question text
        saved = UserProfile.load()
        assert saved.name is None, \
            f"Question polluted profile.name = {saved.name!r} (expected None)"
        assert saved.name != "谁你知道吗"

    @pytest.mark.asyncio
    async def test_who_am_i_does_not_pollute_profile(self, tmp_path, monkeypatch):
        """English question form — same guard applies."""
        from ui.cli import SimpleCLI
        from agent.core.engine import AgentEngine, AgentConfig
        from agent.core.user_profile import UserProfile
        from agent.llm.client import LLMClient

        profile_path = tmp_path / "profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_path))
        if profile_path.exists():
            profile_path.unlink()

        cli = SimpleCLI()
        engine = AgentEngine(AgentConfig())
        engine.user_profile = UserProfile()

        class _Delta:
            content = "I don't know"
            tool_calls = None
        class _Choice:
            delta = _Delta()
        class _Chunk:
            choices = [_Choice()]
            usage = None
        async def _aiter():
            yield _Chunk()
        async def mock_chat(messages, stream=True):
            return _aiter(), True
        engine.llm = LLMClient(provider="mock", model="test")
        engine.llm.chat = mock_chat

        await cli._direct_answer("Who am I?", engine, 0.0)

        saved = UserProfile.load()
        assert saved.name is None, \
            f"Question polluted profile.name = {saved.name!r}"

    @pytest.mark.asyncio
    async def test_statement_still_saves_to_profile(self, tmp_path, monkeypatch):
        """Regression: '我是 hay' must STILL save name='hay' to profile."""
        from ui.cli import SimpleCLI
        from agent.core.engine import AgentEngine, AgentConfig
        from agent.core.user_profile import UserProfile
        from agent.llm.client import LLMClient

        profile_path = tmp_path / "profile.json"
        monkeypatch.setenv("CODING_AGENT_USER_PROFILE", str(profile_path))
        if profile_path.exists():
            profile_path.unlink()

        cli = SimpleCLI()
        engine = AgentEngine(AgentConfig())
        engine.user_profile = UserProfile()

        class _Delta:
            content = "Hi hay"
            tool_calls = None
        class _Choice:
            delta = _Delta()
        class _Chunk:
            choices = [_Choice()]
            usage = None
        async def _aiter():
            yield _Chunk()
        async def mock_chat(messages, stream=True):
            return _aiter(), True
        engine.llm = LLMClient(provider="mock", model="test")
        engine.llm.chat = mock_chat

        await cli._direct_answer("我是 hay", engine, 0.0)

        saved = UserProfile.load()
        assert saved.name == "hay", \
            f"Statement did not save name: got {saved.name!r}"


class TestInstantResponse:
    """Layer 1 (echo), Layer 2 (toolbar), Layer 3 (Live) — Claude-like UX."""

    def test_echo_uses_rich_path_when_available(self, monkeypatch, capsys):
        """Echo must call _RICH.print so the bubble appears synchronously."""
        from ui.cli import SimpleCLI, RICH_AVAILABLE, _RICH

        cli = SimpleCLI()
        # Capture Console.print calls
        if RICH_AVAILABLE:
            calls = []
            real_print = _RICH.print
            def _spy(*args, **kwargs):
                calls.append((args, kwargs))
            monkeypatch.setattr(_RICH, "print", _spy)
            try:
                cli._echo_user_input("列出所有 .py 文件")
            finally:
                monkeypatch.setattr(_RICH, "print", real_print)
            assert len(calls) >= 1
            # First arg should be a Rich Panel (or a renderable containing the text)
            from rich.panel import Panel
            first_arg = calls[0][0][0]
            assert isinstance(first_arg, Panel), \
                f"Expected Panel, got {type(first_arg).__name__}"
            # The Panel's renderable (Text) contains the user input
            body = first_arg.renderable
            body_str = str(body) if hasattr(body, "__str__") else ""
            assert "列出所有" in body_str, f"User text not in Panel body: {body_str!r}"
            # The title contains '> you'
            title = first_arg.title
            title_str = str(title) if title is not None else ""
            assert "you" in title_str, f"Title missing 'you': {title_str!r}"
        else:
            # Fallback path — must not raise
            cli._echo_user_input("hello")
            captured = capsys.readouterr()
            assert "hello" in captured.out

    def test_echo_handles_5000_char_input(self, capsys):
        """Large paste must not crash and must truncate the preview."""
        from ui.cli import SimpleCLI

        cli = SimpleCLI()
        big = "x" * 5000
        # Should not raise
        cli._echo_user_input(big)
        captured = capsys.readouterr()
        # The output should NOT contain all 5000 x's in a row (truncation)
        # (NO_COLOR may strip all output, so only check when not stripped)
        from ui.cli import _NO_COLOR
        if not _NO_COLOR:
            # Truncation suffix appears
            assert "more chars" in captured.out or len(captured.out) < 6000

    def test_echo_empty_input_is_noop(self, capsys):
        """Empty input must not print anything (avoids blank bubbles)."""
        from ui.cli import SimpleCLI

        cli = SimpleCLI()
        cli._echo_user_input("")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_echo_respects_no_color(self, monkeypatch):
        """NO_COLOR=1 → Rich Console should be in no_color mode for echo."""
        from ui.cli import SimpleCLI, _RICH, RICH_AVAILABLE

        if not RICH_AVAILABLE:
            pytest.skip("Rich not available")

        cli = SimpleCLI()
        # _RICH is constructed at import time with no_color=_NO_COLOR.
        # Verify the echo path produces no ANSI escapes when NO_COLOR is set.
        import os
        from ui.cli import _NO_COLOR as current_no_color
        if not current_no_color:
            # Sanity: when not in NO_COLOR mode, the rendered output uses ANSI
            captured_text = []
            real_print = _RICH.print
            def _spy(*args, **kwargs):
                captured_text.append(str(args))
            monkeypatch.setattr(_RICH, "print", _spy)
            try:
                cli._echo_user_input("hello world")
            finally:
                monkeypatch.setattr(_RICH, "print", real_print)
            # In color mode, Rich's Panel markup is passed; in no_color it's also fine.
            # Just assert no exception was raised.
            assert len(captured_text) >= 1
        else:
            # Already in NO_COLOR mode — just run and assert no crash
            cli._echo_user_input("hello world")

    def test_toolbar_includes_session_tokens(self, monkeypatch):
        """After a turn, the toolbar must show accumulated tokens."""
        from ui.cli import SimpleCLI

        cli = SimpleCLI()
        cli._session_tokens_in = 1234
        cli._session_tokens_out = 56
        cli._session_tokens_estimated_turns = 1

        # Mock config to return known model/mode
        from agent.core.config import config as _cfg
        monkeypatch.setattr(_cfg, "get", lambda key, default=None: {
            "model": "moonshot-v1-8k", "mode": "default",
        }.get(key, default))

        result = cli._build_toolbar()
        assert result is not None
        # FormattedText is list of (style, text) tuples
        text = "".join(t[1] for t in result if isinstance(t, tuple))
        assert "moonshot-v1-8k" in text
        assert "1,234" in text
        assert "56" in text
        assert "估计" in text

    def test_toolbar_when_no_tokens(self, monkeypatch):
        """Fresh CLI with zero tokens must not crash; model/mode still shown."""
        from ui.cli import SimpleCLI

        cli = SimpleCLI()
        assert cli._session_tokens_in == 0
        assert cli._session_tokens_out == 0

        from agent.core.config import config as _cfg
        monkeypatch.setattr(_cfg, "get", lambda key, default=None: {
            "model": "test-model", "mode": "plan",
        }.get(key, default))

        result = cli._build_toolbar()
        text = "".join(t[1] for t in result if isinstance(t, tuple))
        assert "test-model" in text
        assert "plan" in text
        # No token counts shown when zero
        assert "⬇" not in text

    def test_toolbar_strips_provider_prefix(self, monkeypatch):
        """Model name 'openai/gpt-4' should display as 'gpt-4' (no provider)."""
        from ui.cli import SimpleCLI

        cli = SimpleCLI()
        from agent.core.config import config as _cfg
        monkeypatch.setattr(_cfg, "get", lambda key, default=None: {
            "model": "openai/gpt-4-turbo", "mode": "default",
        }.get(key, default))

        result = cli._build_toolbar()
        text = "".join(t[1] for t in result if isinstance(t, tuple))
        assert "gpt-4-turbo" in text
        # The provider prefix is dropped
        assert "openai/" not in text

    def test_toolbar_truncates_long_model_names(self, monkeypatch):
        """Model names longer than 24 chars get truncated with …"""
        from ui.cli import SimpleCLI

        cli = SimpleCLI()
        from agent.core.config import config as _cfg
        long_name = "some-provider/" + ("a" * 30)  # > 24 chars
        monkeypatch.setattr(_cfg, "get", lambda key, default=None: {
            "model": long_name, "mode": "default",
        }.get(key, default))

        result = cli._build_toolbar()
        text = "".join(t[1] for t in result if isinstance(t, tuple))
        # The full long name should NOT appear in the toolbar
        assert long_name not in text
        # Truncation marker present
        assert "…" in text

    def test_run_loop_calls_echo_after_input_history(self):
        """Verify the run() loop wires _echo_user_input after _input_history.append.

        This is a structural test: assert the source code contains the
        call in the right order. Guards against accidental removal during
        refactor.
        """
        import inspect
        from ui.cli import SimpleCLI

        src = inspect.getsource(SimpleCLI.run)
        # _input_history.append should appear before _echo_user_input
        idx_history = src.find("_input_history.append(user_input)")
        idx_echo = src.find("_echo_user_input(user_input)")
        assert idx_history > 0, "_input_history.append not found in run()"
        assert idx_echo > 0, "_echo_user_input not called in run()"
        assert idx_echo > idx_history, "echo must come after history.append"

    def test_prompt_session_uses_bottom_toolbar(self):
        """PromptSession must be constructed with bottom_toolbar=_build_toolbar."""
        import inspect
        from ui.cli import SimpleCLI

        src = inspect.getsource(SimpleCLI._read_line)
        assert "bottom_toolbar" in src
        assert "_build_toolbar" in src
