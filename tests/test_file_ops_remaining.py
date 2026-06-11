"""Tests for remaining file ops tools: write_file, list_files."""

import asyncio

import pytest

from agent.tools.file_ops import WriteFileTool, ListFilesTool


def _run(async_fn):
    return asyncio.run(async_fn)


class TestWriteFile:
    def test_creates_file(self, tmp_path):
        tool = WriteFileTool()
        result = _run(tool.execute(
            path=str(tmp_path / "new.txt"),
            content="hello world",
        ))
        assert result.success is True
        assert (tmp_path / "new.txt").read_text() == "hello world"

    def test_creates_parent_dirs(self, tmp_path):
        tool = WriteFileTool()
        result = _run(tool.execute(
            path=str(tmp_path / "a" / "b" / "c.txt"),
            content="nested",
        ))
        assert result.success is True
        assert (tmp_path / "a" / "b" / "c.txt").read_text() == "nested"

    def test_overwrites_existing(self, tmp_path):
        (tmp_path / "existing.txt").write_text("old")
        tool = WriteFileTool()
        result = _run(tool.execute(
            path=str(tmp_path / "existing.txt"),
            content="new",
        ))
        assert result.success is True
        assert (tmp_path / "existing.txt").read_text() == "new"


class TestListFiles:
    def test_lists_files_and_dirs(self, tmp_path):
        (tmp_path / "file1.txt").write_text("a")
        (tmp_path / "file2.py").write_text("b")
        (tmp_path / "subdir").mkdir()

        tool = ListFilesTool()
        result = _run(tool.execute(path=str(tmp_path)))
        assert result.success is True
        assert "[file] file1.txt" in result.content
        assert "[file] file2.py" in result.content
        assert "[dir] subdir" in result.content

    def test_empty_directory(self, tmp_path):
        tool = ListFilesTool()
        result = _run(tool.execute(path=str(tmp_path)))
        assert result.success is True
        assert result.content == "(empty)"

    def test_nonexistent_dir(self, tmp_path):
        tool = ListFilesTool()
        result = _run(tool.execute(path=str(tmp_path / "nope")))
        assert result.success is False
        assert "not found" in result.error
