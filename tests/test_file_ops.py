"""Tests for Phase 4: Precise file editing tools."""

import asyncio

import pytest

from agent.tools.file_ops import ApplyDiffTool, InsertAfterLineTool, ReadFileTool, ReplaceLinesTool


def _run(async_fn):
    return asyncio.run(async_fn)


class TestApplyDiff:
    def test_search_replace_success(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    print('hello')\n    return 42\n")

        tool = ApplyDiffTool()
        result = _run(tool.execute(
            path=str(f),
            search="    print('hello')",
            replace="    print('hello')\n    print('extra')",
        ))
        assert result.success is True
        assert "Applied diff" in result.content
        assert "+    print('extra')" in result.content
        # Verify file was actually changed
        content = f.read_text()
        assert "extra" in content

    def test_search_not_found(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def hello(): pass\n")

        tool = ApplyDiffTool()
        result = _run(tool.execute(
            path=str(f),
            search="not_in_file",
            replace="replacement",
        ))
        assert result.success is False
        assert "not found" in result.error


class TestInsertAfterLine:
    def test_insert_at_line(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")

        tool = InsertAfterLineTool()
        result = _run(tool.execute(path=str(f), line=1, content="inserted"))
        assert result.success is True
        lines = f.read_text().splitlines()
        assert lines[1] == "inserted"


class TestReplaceLines:
    def test_replace_range(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("a\nb\nc\nd\n")

        tool = ReplaceLinesTool()
        result = _run(tool.execute(path=str(f), start=2, end=3, content="X"))
        assert result.success is True
        lines = f.read_text().splitlines()
        assert lines == ["a", "X", "d"]


class TestReadFile:
    def test_line_numbers(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("first\nsecond\nthird\n")

        tool = ReadFileTool()
        result = _run(tool.execute(path=str(f)))
        assert result.success is True
        assert "1 | first" in result.content
        assert "2 | second" in result.content

    def test_offset_and_limit(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("a\nb\nc\nd\n")

        tool = ReadFileTool()
        result = _run(tool.execute(path=str(f), offset=2, limit=2))
        assert result.success is True
        assert "2 | b" in result.content
        assert "3 | c" in result.content
        assert "1 | a" not in result.content
