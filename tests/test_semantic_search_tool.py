"""Tests for the semantic_search tool (PR-04)."""

import json
import pytest

from agent.tools.memory import semantic_search_tool
from agent.core.vector_memory import reset_vector_memory


@pytest.fixture(autouse=True)
def clean_memory():
    """Reset the singleton before each test."""
    reset_vector_memory()
    yield
    reset_vector_memory()


# ── Tool registration ────────────────────────────────────────────


class TestToolRegistration:
    def test_tool_registered(self):
        from agent.tools.base import registry
        assert registry.get("semantic_search") is semantic_search_tool

    def test_tool_name(self):
        assert semantic_search_tool.name == "semantic_search"

    def test_tool_is_read_only(self):
        assert semantic_search_tool.is_read_only is True

    def test_tool_is_concurrency_safe(self):
        assert semantic_search_tool.is_concurrency_safe is True

    def test_tool_schema_is_wrapped(self):
        schema = semantic_search_tool.schema
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "semantic_search"

    def test_schema_requires_query(self):
        schema = semantic_search_tool.schema
        params = schema["function"]["parameters"]
        assert "query" in params["required"]

    def test_schema_k_has_bounds(self):
        schema = semantic_search_tool.schema
        k_schema = schema["function"]["parameters"]["properties"]["k"]
        assert k_schema["minimum"] == 1
        assert k_schema["maximum"] == 50


# ── Execution ────────────────────────────────────────────────────


class TestExecution:
    @pytest.mark.asyncio
    async def test_empty_query_fails(self):
        result = await semantic_search_tool.execute(query="")
        assert not result.success
        assert "non-empty" in (result.error or "")

    @pytest.mark.asyncio
    async def test_whitespace_query_fails(self):
        result = await semantic_search_tool.execute(query="   ")
        assert not result.success

    @pytest.mark.asyncio
    async def test_k_out_of_range_fails(self):
        result = await semantic_search_tool.execute(query="x", k=0)
        assert not result.success
        result = await semantic_search_tool.execute(query="x", k=100)
        assert not result.success

    @pytest.mark.asyncio
    async def test_empty_memory_returns_empty_hits(self):
        result = await semantic_search_tool.execute(query="anything")
        assert result.success
        payload = json.loads(result.content)
        assert payload["count"] == 0
        assert payload["hits"] == []

    @pytest.mark.asyncio
    async def test_search_returns_hits(self):
        from agent.core.vector_memory import get_vector_memory
        vm = get_vector_memory()
        vm.add("python", "def hello(): print('world')")
        vm.add("javascript", "function hello() { console.log('world') }")
        vm.add("cooking", "how to bake sourdough bread")
        result = await semantic_search_tool.execute(query="hello function", k=2)
        assert result.success
        payload = json.loads(result.content)
        assert payload["count"] == 2
        assert payload["query"] == "hello function"
        assert all("score" in h for h in payload["hits"])

    @pytest.mark.asyncio
    async def test_metadata_excluded_by_default(self):
        from agent.core.vector_memory import get_vector_memory
        vm = get_vector_memory()
        vm.add("k", "v", metadata={"src": "test"})
        result = await semantic_search_tool.execute(query="v", k=1)
        payload = json.loads(result.content)
        assert "metadata" not in payload["hits"][0]

    @pytest.mark.asyncio
    async def test_metadata_included_when_requested(self):
        from agent.core.vector_memory import get_vector_memory
        vm = get_vector_memory()
        vm.add("k", "v", metadata={"src": "test"})
        result = await semantic_search_tool.execute(
            query="v", k=1, include_metadata=True
        )
        payload = json.loads(result.content)
        assert "metadata" in payload["hits"][0]
        assert payload["hits"][0]["metadata"] == {"src": "test"}


# ── Integration with global registry ─────────────────────────────


class TestGlobalRegistration:
    def test_importing_tools_registers_semantic_search(self):
        # Importing agent.tools triggers all registrations
        import agent.tools  # noqa: F401
        from agent.tools.base import registry
        assert registry.get("semantic_search") is not None
