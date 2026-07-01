"""Tests for Claude Code-aligned CLI output rendering.

Covers two alignment changes:
  1. The per-turn ``── ⬇ ... · Xs`` token/elapsed footer is OFF by default
     (``AGENT_SHOW_TOKEN_TAIL`` unset) so prose stays clean, like Claude Code.
  2. Tool call/result lines show the tool name exactly once — no
     ``Read · Read · src/x`` duplication.
"""

import importlib


def _reload_cli(monkeypatch, env_value):
    """Reload ui.cli with a given AGENT_SHOW_TOKEN_TAIL env value.

    The module reads the env var at import time into _SHOW_TOKEN_TAIL, so we
    must reload to pick up a changed value.
    """
    monkeypatch.setenv("AGENT_SHOW_TOKEN_TAIL", env_value)
    import ui.cli as cli

    importlib.reload(cli)
    return cli


class TestTokenTailDefault:
    """The inline token/elapsed footer must be off by default."""

    def test_footer_off_by_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_SHOW_TOKEN_TAIL", raising=False)
        cli = _reload_cli(monkeypatch, "")
        assert cli._SHOW_TOKEN_TAIL is False

    def test_footer_on_when_opted_in(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "1")
        assert cli._SHOW_TOKEN_TAIL is True


class TestStripToolPrefix:
    """``_strip_tool_prefix`` removes the redundant leading tool-name label.

    Tools render their own call/result as ``"Read · src/x"`` but the CLI already
    prints the badge beside it — so we strip to avoid ``Read · Read · src/x``.
    """

    def test_strips_read_prefix(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        assert cli._strip_tool_prefix("Read · src/Hero.tsx", "Read") == "src/Hero.tsx"

    def test_strips_read_with_lines(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        assert cli._strip_tool_prefix("Read · 42 lines", "Read") == "42 lines"

    def test_strips_glob_prefix(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        assert cli._strip_tool_prefix("Glob · 3 files", "Glob") == "3 files"

    def test_strips_bash_result_with_multiple_segments(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        # "Bash · 0.3s · 12 lines" → "0.3s · 12 lines" (only the name stripped)
        assert cli._strip_tool_prefix("Bash · 0.3s · 12 lines", "Bash") == "0.3s · 12 lines"

    def test_no_strip_when_no_prefix(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        # A raw command (shell render_call) is not prefixed — must stay intact.
        assert cli._strip_tool_prefix("npx serve out", "Bash") == "npx serve out"

    def test_no_strip_unrelated_name(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        assert cli._strip_tool_prefix("Read · 42 lines", "Bash") == "Read · 42 lines"

    def test_empty_inputs(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        assert cli._strip_tool_prefix("", "Read") == ""
        assert cli._strip_tool_prefix("Read · x", "") == "Read · x"


class TestToolIconNoDuplicate:
    """The call line must not repeat the tool name in the label."""

    def test_read_call_label_has_no_read_prefix(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        icon, label = cli._tool_icon("read_file", {"path": "src/Hero.tsx"})
        # Badge shows the tool name; the label is the bare path.
        assert "Read" in icon
        assert label == "src/Hero.tsx"
        # No doubled "Read · Read" anywhere in the combined line.
        combined = f"{icon} · {label}"
        assert "Read · Read" not in combined
