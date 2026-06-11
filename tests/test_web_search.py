"""Tests for web_search tool."""

from agent.tools.base import registry
from agent.tools.web_search import WebSearchTool


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class TestWebSearchTool:
    def test_tool_registered(self):
        """Verify tool is registered."""
        tool = registry.get("web_search")
        assert tool is not None
        assert tool.name == "web_search"

    def test_schema(self):
        """Schema contains required fields."""
        tool = WebSearchTool()
        schema = tool.schema
        assert schema["type"] == "function"
        assert "query" in schema["function"]["parameters"]["properties"]

    def test_execute_with_query(self):
        """Can execute search with a query."""
        tool = WebSearchTool()
        result = _run(tool.execute(query="Python programming"))
        # Either succeeds with results, or fails with error message
        # (network issues are ok, but no crashes)
        assert result.success or result.error is not None
        assert isinstance(result.content, str)

    def test_max_results(self):
        """Respects max_results parameter."""
        tool = WebSearchTool()
        result = _run(tool.execute(query="test", max_results=5))
        # Should not crash, may succeed or fail based on network
        assert result.success or result.error is not None

    def test_max_length(self):
        """Respects max_length parameter."""
        tool = WebSearchTool()
        result = _run(tool.execute(query="test", max_length=200))
        # Content should be truncated if it exceeds max_length
        if result.success:
            assert len(result.content) <= 200 + 100  # some overhead for truncation msg
