"""Tests for web_fetch tool."""

import asyncio

from agent.tools.web_fetch import WebFetchTool


def _run(async_fn):
    return asyncio.run(async_fn)


class TestWebFetchTool:
    def test_fetch_example_com(self):
        tool = WebFetchTool()
        result = _run(tool.execute(url="https://example.com", max_length=500))
        assert result.success is True
        assert "Example Domain" in result.content

    def test_invalid_url(self):
        tool = WebFetchTool()
        result = _run(tool.execute(url="not-a-valid-url"))
        assert result.success is False

    def test_truncation(self):
        tool = WebFetchTool()
        result = _run(tool.execute(url="https://example.com", max_length=50))
        assert result.success is True
        assert "truncated" in result.content
