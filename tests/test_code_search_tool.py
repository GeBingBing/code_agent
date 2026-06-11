"""Tests for Phase 6: Code search tool."""

import asyncio
import tempfile

import pytest

from agent.tools.code_search import CodeSearchTool


def _run(async_fn):
    return asyncio.run(async_fn)


class TestCodeSearchTool:
    def test_search_finds_symbols(self, tmp_path, monkeypatch):
        # Create a Python file BEFORE initializing the tool
        (tmp_path / "auth.py").write_text("class UserAuth:\n    def login(self): pass\n")

        tool = CodeSearchTool(str(tmp_path))
        result = _run(tool.execute(query="UserAuth", top_k=5))
        assert result.success is True
        assert "UserAuth" in result.content
        assert "auth.py" in result.content

    def test_search_no_results(self, tmp_path, monkeypatch):
        tool = CodeSearchTool(str(tmp_path))
        result = _run(tool.execute(query="nonexistent_xyz", top_k=5))
        assert result.success is True
        assert "No results" in result.content
