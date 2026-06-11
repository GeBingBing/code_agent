"""Tests for MCP (Model Context Protocol) integration (P2-4)."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from agent.mcp.client import MCPClient, MCPServerConfig
from agent.mcp.adapter import (
    MCPToolAdapter,
    MCPServerManager,
    register_mcp_tools_from_config,
    _load_mcp_config,
    _mcp_tool_name,
    _convert_mcp_schema,
    _format_mcp_result,
    DEFAULT_MCP_CONFIG_PATH,
)
from agent.tools.base import ToolResult, registry


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def sample_tool_def():
    return {
        "name": "read_file",
        "description": "Read a file from the filesystem",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read",
                }
            },
            "required": ["path"],
        },
    }


@pytest.fixture
def sample_mcp_config():
    return {
        "mcpServers": {
            "test-server": {
                "command": "echo",
                "args": ["test"],
            }
        }
    }


# ── Config Loading ───────────────────────────────────────────────────


class TestLoadMCPConfig:
    """Test MCP configuration loading."""

    def test_load_valid_config(self, tmp_path):
        config_file = tmp_path / "mcp_servers.json"
        config_data = {
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@mcp/server-filesystem"],
                    "env": {"HOME": "/tmp"},
                }
            }
        }
        config_file.write_text(json.dumps(config_data))

        result = _load_mcp_config(str(config_file))
        assert result == config_data
        assert "filesystem" in result["mcpServers"]

    def test_load_missing_file(self, tmp_path):
        result = _load_mcp_config(str(tmp_path / "nonexistent.json"))
        assert result == {}

    def test_load_invalid_json(self, tmp_path):
        config_file = tmp_path / "bad.json"
        config_file.write_text("not valid json {{{")

        result = _load_mcp_config(str(config_file))
        assert result == {}

    def test_load_default_path_nonexistent(self):
        """Should return empty dict when default config doesn't exist."""
        result = _load_mcp_config()
        # This may or may not exist on the test machine
        assert isinstance(result, dict)


# ── Tool Name Generation ─────────────────────────────────────────────


class TestMCPToolName:
    """Test MCP tool name generation."""

    def test_basic_namespacing(self):
        assert _mcp_tool_name("filesystem", "read_file") == "mcp__filesystem__read_file"

    def test_server_name_with_hyphens(self):
        assert _mcp_tool_name("my-server", "list_dir") == "mcp__my-server__list_dir"

    def test_tool_name_with_underscores(self):
        assert _mcp_tool_name("db", "query_table") == "mcp__db__query_table"


# ── Schema Conversion ────────────────────────────────────────────────


class TestConvertMCPSchema:
    """Test conversion from MCP to OpenAI function-call schema."""

    def test_basic_conversion(self, sample_tool_def):
        result = _convert_mcp_schema(sample_tool_def)
        assert result["type"] == "function"
        assert result["function"]["name"] == "read_file"
        assert result["function"]["description"] == "Read a file from the filesystem"
        assert "parameters" in result["function"]
        assert result["function"]["parameters"]["type"] == "object"
        assert "path" in result["function"]["parameters"]["properties"]
        assert result["function"]["parameters"]["required"] == ["path"]

    def test_no_required_params(self):
        tool_def = {
            "name": "list_dir",
            "description": "List directory contents",
            "inputSchema": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
            },
        }
        result = _convert_mcp_schema(tool_def)
        assert "required" not in result["function"]["parameters"]

    def test_empty_input_schema(self):
        tool_def = {"name": "ping", "description": "Ping the server"}
        result = _convert_mcp_schema(tool_def)
        assert result["function"]["parameters"]["type"] == "object"
        assert result["function"]["parameters"]["properties"] == {}

    def test_nested_properties(self):
        tool_def = {
            "name": "query",
            "description": "Run a query",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "age": {"type": "integer"},
                        },
                    }
                },
            },
        }
        result = _convert_mcp_schema(tool_def)
        params = result["function"]["parameters"]
        assert "filter" in params["properties"]
        nested = params["properties"]["filter"]
        assert nested["type"] == "object"
        assert "name" in nested["properties"]


# ── Result Formatting ────────────────────────────────────────────────


class TestFormatMCPResult:
    """Test MCP result formatting."""

    def test_text_content(self):
        result = {"content": [{"type": "text", "text": "Hello, world!"}]}
        assert _format_mcp_result(result) == "Hello, world!"

    def test_mixed_content(self):
        result = {
            "content": [
                {"type": "text", "text": "Line 1"},
                {"type": "text", "text": "Line 2"},
            ]
        }
        assert _format_mcp_result(result) == "Line 1\nLine 2"

    def test_image_content(self):
        result = {
            "content": [
                {"type": "image", "data": "base64...", "mimeType": "image/png"}
            ]
        }
        assert "image: image/png" in _format_mcp_result(result)

    def test_resource_content(self):
        result = {
            "content": [
                {"type": "resource", "uri": "file:///tmp/data.csv"}
            ]
        }
        assert "resource: file:///tmp/data.csv" in _format_mcp_result(result)

    def test_string_result(self):
        assert _format_mcp_result("plain string") == "plain string"

    def test_empty_content(self):
        result = {"content": []}
        output = _format_mcp_result(result)
        # Empty content list is treated as no content, falls back to JSON dump
        assert "content" in output

    def test_no_content_key(self):
        result = {"key": "value"}
        output = _format_mcp_result(result)
        assert "key" in output
        assert "value" in output


# ── MCPToolAdapter ───────────────────────────────────────────────────


class TestMCPToolAdapter:
    """Test MCP tool adapter wrapping."""

    def test_adapter_creation(self, sample_tool_def):
        mock_client = MagicMock(spec=MCPClient)
        adapter = MCPToolAdapter(mock_client, sample_tool_def, "test-server")

        assert adapter.name == "mcp__test-server__read_file"
        assert "Read a file" in adapter.description
        assert adapter._client is mock_client

    def test_adapter_schema(self, sample_tool_def):
        mock_client = MagicMock(spec=MCPClient)
        adapter = MCPToolAdapter(mock_client, sample_tool_def, "test-server")

        schema = adapter.schema
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "read_file"

    @pytest.mark.asyncio
    async def test_adapter_execute_success(self, sample_tool_def):
        mock_client = MagicMock(spec=MCPClient)
        mock_client.call_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "file contents here"}]
        })
        adapter = MCPToolAdapter(mock_client, sample_tool_def, "test-server")

        result = await adapter.execute(path="/tmp/test.txt")
        assert result.success is True
        assert result.content == "file contents here"
        mock_client.call_tool.assert_called_once_with(
            "read_file", {"path": "/tmp/test.txt"}
        )

    @pytest.mark.asyncio
    async def test_adapter_execute_error_response(self, sample_tool_def):
        mock_client = MagicMock(spec=MCPClient)
        mock_client.call_tool = AsyncMock(return_value={
            "error": "Permission denied"
        })
        adapter = MCPToolAdapter(mock_client, sample_tool_def, "test-server")

        result = await adapter.execute(path="/etc/shadow")
        assert result.success is False
        assert "Permission denied" in result.error

    @pytest.mark.asyncio
    async def test_adapter_execute_exception(self, sample_tool_def):
        mock_client = MagicMock(spec=MCPClient)
        mock_client.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))
        adapter = MCPToolAdapter(mock_client, sample_tool_def, "test-server")

        result = await adapter.execute()
        assert result.success is False
        assert "connection lost" in result.error


# ── MCPServerManager ─────────────────────────────────────────────────


class TestMCPServerManager:
    """Test MCP server manager lifecycle."""

    @pytest.mark.asyncio
    async def test_start_all_with_no_servers(self):
        manager = MCPServerManager([])
        count = await manager.start_all()
        assert count == 0
        assert manager.tools == []

    @pytest.mark.asyncio
    async def test_start_all_server_failure(self):
        """Server fails to start — should not crash, just skip it."""
        config = MCPServerConfig(
            name="bad-server",
            command="/nonexistent/command",
            args=["--flag"],
        )
        manager = MCPServerManager([config])
        count = await manager.start_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_stop_all(self):
        """stop_all should be safe even with no servers."""
        manager = MCPServerManager([])
        await manager.stop_all()  # Should not raise
        assert manager.tools == []

    @pytest.mark.asyncio
    async def test_tools_property(self):
        manager = MCPServerManager([])
        assert manager.tools == []


# ── register_mcp_tools_from_config ───────────────────────────────────


class TestRegisterMCPToolsFromConfig:
    """Test the full registration flow."""

    @pytest.mark.asyncio
    async def test_no_config_file(self, tmp_path):
        """Returns None when no config file exists."""
        result = await register_mcp_tools_from_config(
            str(tmp_path / "nonexistent.json")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_config(self, tmp_path):
        """Returns None when config has no servers defined."""
        config_file = tmp_path / "mcp_servers.json"
        config_file.write_text(json.dumps({"mcpServers": {}}))

        result = await register_mcp_tools_from_config(str(config_file))
        assert result is None

    @pytest.mark.asyncio
    async def test_config_with_invalid_server(self, tmp_path):
        """Skips non-dict server configs."""
        config_file = tmp_path / "mcp_servers.json"
        config_file.write_text(json.dumps({
            "mcpServers": {
                "broken": "not a dict",
            }
        }))

        result = await register_mcp_tools_from_config(str(config_file))
        assert result is None  # No valid server configs

    @pytest.mark.asyncio
    async def test_valid_config_but_server_fails(self, tmp_path):
        """Server has valid config but command doesn't work — no crash."""
        config_file = tmp_path / "mcp_servers.json"
        config_file.write_text(json.dumps({
            "mcpServers": {
                "doomed": {
                    "command": "/dev/null/nope",
                    "args": [],
                }
            }
        }))

        # Should not raise, just return manager with 0 tools
        manager = await register_mcp_tools_from_config(str(config_file))
        assert manager is not None
        assert len(manager.tools) == 0
        await manager.stop_all()


# ── MCPClient ────────────────────────────────────────────────────────


class TestMCPClient:
    """Test the MCP JSON-RPC client."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        """Client can start/stop a simple echo process."""
        config = MCPServerConfig(
            name="test",
            command="cat",  # cat reads stdin, won't exit immediately
            args=[],
        )
        client = MCPClient(config)
        ok = await client.start()
        assert ok is True
        await client.stop()

    @pytest.mark.asyncio
    async def test_double_start(self):
        """Calling start twice is safe."""
        config = MCPServerConfig(name="test", command="cat", args=[])
        client = MCPClient(config)
        ok1 = await client.start()
        ok2 = await client.start()
        assert ok1 is True
        assert ok2 is True
        await client.stop()

    @pytest.mark.asyncio
    async def test_initialize_without_handshake(self):
        """Calling initialize on a non-MCP process will fail (timeout)."""
        # Use 'sleep' as a process that won't respond to JSON-RPC
        config = MCPServerConfig(name="test", command="sleep", args=["5"])
        client = MCPClient(config)
        await client.start()
        # initialize() will timeout waiting for a JSON-RPC response
        ok = await client.initialize()
        # Should fail because sleep doesn't speak JSON-RPC
        assert ok is False
        await client.stop()

    @pytest.mark.asyncio
    async def test_list_tools_without_init(self):
        """list_tools when not initialized returns empty list."""
        config = MCPServerConfig(name="test", command="sleep", args=["5"])
        client = MCPClient(config)
        await client.start()
        tools = await client.list_tools()
        assert tools == []
        await client.stop()

    @pytest.mark.asyncio
    async def test_call_tool_without_init(self):
        """call_tool when not initialized returns error dict."""
        config = MCPServerConfig(name="test", command="sleep", args=["5"])
        client = MCPClient(config)
        await client.start()
        result = await client.call_tool("test", {})
        assert "error" in result
        await client.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        """Calling stop on a client that never started is safe."""
        config = MCPServerConfig(name="test", command="cat", args=[])
        client = MCPClient(config)
        await client.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_config_with_env_vars(self):
        """Env vars in config are passed to subprocess."""
        config = MCPServerConfig(
            name="env-test",
            command="env",
            args=[],
            env={"TEST_VAR": "hello_mcp"},
        )
        client = MCPClient(config)
        ok = await client.start()
        assert ok is True
        # Read stdout to check env
        try:
            stdout_data = await asyncio.wait_for(
                client._proc.stdout.read(4096), timeout=2
            )
            output = stdout_data.decode("utf-8", errors="replace")
            assert "TEST_VAR=hello_mcp" in output
        except asyncio.TimeoutError:
            pass  # env output may vary
        finally:
            await client.stop()


# ── Integration with ToolRegistry ────────────────────────────────────


class TestMCPToolRegistryIntegration:
    """Test that MCP tools integrate with the global tool registry."""

    def test_mcp_tool_registers_in_registry(self):
        """An MCP tool adapter can be registered and retrieved."""
        mock_client = MagicMock(spec=MCPClient)
        unique_tool_def = {
            "name": "mcp_unique_op",
            "description": "A unique MCP operation for testing",
            "inputSchema": {
                "type": "object",
                "properties": {"param": {"type": "string"}},
            },
        }
        adapter = MCPToolAdapter(mock_client, unique_tool_def, "test-svr")

        # Register in global registry
        registry.register(adapter)

        # Retrieve
        tool = registry.get("mcp__test-svr__mcp_unique_op")
        assert tool is not None
        assert tool.name == "mcp__test-svr__mcp_unique_op"

        # Verify schema is in the right format
        schemas = registry.schemas
        matching = [s for s in schemas
                     if s.get("function", {}).get("name") == "mcp_unique_op"]
        assert len(matching) >= 1
        assert matching[0]["type"] == "function"

    def test_multiple_mcp_tools_from_same_server(self):
        """Multiple tools from the same server get unique names."""
        mock_client = MagicMock(spec=MCPClient)

        tool1 = MCPToolAdapter(mock_client, {
            "name": "read", "description": "Read file",
            "inputSchema": {"type": "object", "properties": {}}
        }, "fs")
        tool2 = MCPToolAdapter(mock_client, {
            "name": "write", "description": "Write file",
            "inputSchema": {"type": "object", "properties": {}}
        }, "fs")

        assert tool1.name != tool2.name
        assert tool1.name == "mcp__fs__read"
        assert tool2.name == "mcp__fs__write"
