"""Tests for grep tool."""

import asyncio

from agent.tools.grep import GrepTool


def _run(async_fn):
    return asyncio.run(async_fn)


class TestGrepTool:
    def test_finds_pattern(self, tmp_path):
        (tmp_path / "a.py").write_text("def hello():\n    print('world')\n")
        (tmp_path / "b.py").write_text("class Hello:\n    pass\n")

        tool = GrepTool(root_dir=str(tmp_path))
        result = _run(tool.execute(pattern="hello", glob="*.py"))
        assert result.success is True
        assert "a.py" in result.content
        assert "b.py" in result.content

    def test_case_sensitive(self, tmp_path):
        (tmp_path / "x.py").write_text("Hello\nhello\n")

        tool = GrepTool(root_dir=str(tmp_path))
        result = _run(tool.execute(pattern="Hello", case_sensitive=True))
        assert result.success is True
        lines = [l for l in result.content.splitlines() if l.startswith("x.py")]
        assert len(lines) == 1  # only exact case match

    def test_no_matches(self, tmp_path):
        tool = GrepTool(root_dir=str(tmp_path))
        result = _run(tool.execute(pattern="nonexistent_xyz"))
        assert result.success is True
        assert "No matches" in result.content

    def test_max_results(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text("target\n")

        tool = GrepTool(root_dir=str(tmp_path))
        result = _run(tool.execute(pattern="target", max_results=3))
        assert result.success is True
        assert "showing first 3" in result.content

    def test_invalid_regex(self, tmp_path):
        tool = GrepTool(root_dir=str(tmp_path))
        result = _run(tool.execute(pattern="[invalid"))
        assert result.success is False
        assert "Invalid regex" in result.error

    def test_skips_ignored_dirs(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "cache.pyc").write_text("target\n")
        (tmp_path / "main.py").write_text("target\n")

        tool = GrepTool(root_dir=str(tmp_path))
        result = _run(tool.execute(pattern="target"))
        assert result.success is True
        lines = [l for l in result.content.splitlines() if l.startswith("main.py")]
        assert len(lines) == 1
