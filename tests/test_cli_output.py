"""Tests for Claude Code-aligned CLI output rendering.

Covers:
  1. The per-turn ``── ⬇ ... · Xs`` token/elapsed footer is OFF by default.
  2. Tool call/result lines show the tool name exactly once (no Read · Read).
  3. Paths are shown relative to the workspace root.
  4. list_files shows just the path on the call line and a file count on the
     result line — not ``list_files: path=...`` / ``.DS_Store``.
"""

import importlib


def _reload_cli(monkeypatch, env_value=""):
    """Reload ui.cli (and agent.core.workspace) to pick up env-var-driven state.

    ``ui.cli`` reads ``AGENT_SHOW_TOKEN_TAIL`` at import time; ``_relpath`` reads
    ``WORKSPACE_ROOT`` (which itself reads ``CODING_AGENT_WORKSPACE`` at import).
    Both must be reloaded after setting the env var.
    """
    if env_value:
        monkeypatch.setenv("AGENT_SHOW_TOKEN_TAIL", env_value)
    import agent.core.workspace as workspace

    importlib.reload(workspace)
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
    """``_strip_tool_prefix`` removes the redundant leading tool-name label."""

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
        assert cli._strip_tool_prefix("Bash · 0.3s · 12 lines", "Bash") == "0.3s · 12 lines"

    def test_no_strip_when_no_prefix(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
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
        assert "Read" in icon
        assert label == "src/Hero.tsx"
        combined = f"{icon}  {label}"
        assert "Read · Read" not in combined


class TestCompactCallStyle:
    """Tool call/result lines use the compact Claude Code style:

    call:   ``  Read  src/x.tsx``   (two-space sep, no ``·``)
    result: ``  ⎿ 32 lines``        (indented ⎿, no ✓, no repeated tool name)
    """

    def test_call_line_uses_two_spaces_not_dot_separator(self, monkeypatch):
        import re

        cli = _reload_cli(monkeypatch, "")
        icon, label = cli._tool_icon("read_file", {"path": "src/x.tsx"})
        line = f"{icon}  {label}"
        # Strip ANSI escapes so the assertion sees the visible text.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
        # The middle separator is two spaces, never a '·'.
        assert "·" not in plain
        assert "Read  src/x.tsx" in plain

    def test_result_line_is_indented_hook_no_checkmark(self, monkeypatch, capsys):
        cli = _reload_cli(monkeypatch, "")
        cli._rich_print_tool_result("Read", "32 lines", success=True)
        out = capsys.readouterr().out
        assert "⎿" in out
        assert "32 lines" in out
        # No checkmark, no repeated tool name on the result line.
        assert "✓" not in out
        assert "Read" not in out

    def test_result_line_failure_shows_cross(self, monkeypatch, capsys):
        cli = _reload_cli(monkeypatch, "")
        cli._rich_print_tool_result("Bash", "permission denied", success=False)
        out = capsys.readouterr().out
        assert "⎿" in out
        assert "✗" in out


class TestRelativePaths:
    """Paths are shown relative to the workspace root (Claude Code style)."""

    def test_relpath_strips_workspace_prefix(self, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", "/Users/u/proj")
        cli = _reload_cli(monkeypatch, "")
        assert cli._relpath("/Users/u/proj/src/app/layout.tsx") == "src/app/layout.tsx"

    def test_relpath_workspace_itself_becomes_dot(self, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", "/Users/u/proj")
        cli = _reload_cli(monkeypatch, "")
        assert cli._relpath("/Users/u/proj") == "."

    def test_relpath_keeps_relative_paths(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        assert cli._relpath("src/x.ts") == "src/x.ts"
        assert cli._relpath(".") == "."
        assert cli._relpath("") == ""

    def test_relpath_keeps_unrelated_absolute(self, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", "/Users/u/proj")
        cli = _reload_cli(monkeypatch, "")
        assert cli._relpath("/etc/passwd") == "/etc/passwd"

    def test_relpath_in_text_relativizes_embedded_path(self, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", "/Users/u/proj")
        cli = _reload_cli(monkeypatch, "")
        assert cli._relpath_in_text("Written to /Users/u/proj/src/x.tsx") == "Written to src/x.tsx"

    def test_relpath_in_text_noop_without_path(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        assert cli._relpath_in_text("Read · 42 lines") == "Read · 42 lines"
        assert cli._relpath_in_text("npm run build") == "npm run build"

    def test_tool_icon_relativizes_absolute_path(self, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_WORKSPACE", "/Users/u/proj")
        cli = _reload_cli(monkeypatch, "")
        _, label = cli._tool_icon("read_file", {"path": "/Users/u/proj/src/app/layout.tsx"})
        assert label == "src/app/layout.tsx"


class TestListFilesRendering:
    """list_files shows just the path on the call line and a file count on the
    result line — not ``list_files: path=...`` / ``.DS_Store``."""

    def test_list_call_label_is_bare_path(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        icon, label = cli._tool_icon("list_files", {"path": "src"})
        assert "List" in icon
        assert label == "src"

    def test_list_result_shows_item_count(self):
        from agent.tools.file_ops import ListFilesTool

        tool = ListFilesTool()

        class _R:
            success = True
            content = "[dir] a\n[file] b"
            error = ""
            metadata = {"dirs": 1, "files": 1}

        assert tool.render_result(_R()) == "1 files, 1 dirs"

    def test_write_call_label_is_bare_path(self, monkeypatch):
        cli = _reload_cli(monkeypatch, "")
        icon, label = cli._tool_icon("write_file", {"path": "src/x.tsx"})
        assert label == "src/x.tsx"
        assert "write_file" not in label
        assert "path=" not in label
