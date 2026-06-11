"""MCP Tool Adapter — bridges external MCP servers into the agent's tool registry.

Wraps MCP tools as BaseTool subclasses so they appear as native tools
and can be called transparently through the standard tool execution path.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import MCPClient, MCPServerConfig
from ..tools.base import BaseTool, ToolResult, registry


# Default MCP servers config path
DEFAULT_MCP_CONFIG_PATH = Path.home() / ".coding-agent" / "mcp_servers.json"


def _load_mcp_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load MCP server configuration from a JSON file.

    Expected format:
        {
            "mcpServers": {
                "server-name": {
                    "command": "npx",
                    "args": ["-y", "@scope/server-name"],
                    "env": {"KEY": "value"}
                }
            }
        }
    """
    path = Path(config_path) if config_path else DEFAULT_MCP_CONFIG_PATH
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[MCP] Failed to load config from {path}: {e}")
        return {}


def _mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Generate a namespaced tool name to avoid conflicts."""
    return f"mcp__{server_name}__{tool_name}"


def _convert_mcp_schema(tool_def: Dict[str, Any]) -> Dict[str, Any]:
    """Convert MCP tool definition to OpenAI function-call schema format.

    MCP format:
        {"name": "...", "description": "...", "inputSchema": {...}}

    OpenAI format:
        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    input_schema = tool_def.get("inputSchema", {})
    parameters = {
        "type": input_schema.get("type", "object"),
        "properties": input_schema.get("properties", {}),
    }
    if "required" in input_schema:
        parameters["required"] = input_schema["required"]

    return {
        "type": "function",
        "function": {
            "name": tool_def.get("name", ""),
            "description": tool_def.get("description", ""),
            "parameters": parameters,
        },
    }


def _format_mcp_result(result: Dict[str, Any]) -> str:
    """Format MCP tool result into a readable string.

    MCP results use a content array:
        {"content": [{"type": "text", "text": "..."}, ...]}
    """
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result)

    content = result.get("content", [])
    if not content:
        return json.dumps(result, ensure_ascii=False, indent=2)

    parts = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "image":
                parts.append(f"[image: {item.get('mimeType', 'unknown')}]")
            elif item.get("type") == "resource":
                parts.append(f"[resource: {item.get('uri', 'unknown')}]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        else:
            parts.append(str(item))
    return "\n".join(parts)


class MCPToolAdapter(BaseTool):
    """Wraps an MCP tool from an external server as a native agent tool.

    Each adapter holds a reference to the MCPClient so tool calls
    are routed to the correct server.
    """

    def __init__(self, client: MCPClient, tool_def: Dict[str, Any], server_name: str):
        self._client = client
        self._tool_def = tool_def
        self._server_name = server_name
        self._original_name = tool_def.get("name", "unknown")
        self.name = _mcp_tool_name(server_name, self._original_name)
        self.description = tool_def.get("description", f"MCP tool: {self._original_name}")

    @property
    def schema(self) -> dict:
        return _convert_mcp_schema(self._tool_def)

    async def execute(self, **kwargs) -> ToolResult:
        """Execute the MCP tool via the connected server."""
        try:
            result = await self._client.call_tool(self._original_name, kwargs)
            if isinstance(result, dict) and "error" in result:
                return ToolResult(
                    success=False,
                    content="",
                    error=result["error"],
                )
            content = _format_mcp_result(result)
            return ToolResult(success=True, content=content)
        except Exception as e:
            return ToolResult(
                success=False,
                content="",
                error=f"MCP tool '{self._original_name}' failed: {e}",
            )


class MCPServerManager:
    """Manages lifecycle of multiple MCP server connections.

    Handles starting servers, discovering tools, registering adapters,
    and graceful shutdown.
    """

    def __init__(self, servers: List[MCPServerConfig]):
        self._servers: Dict[str, MCPClient] = {}
        self._configs = servers
        self._tools: List[MCPToolAdapter] = []

    @property
    def tools(self) -> List[MCPToolAdapter]:
        """Return all registered MCP tool adapters."""
        return self._tools

    async def start_all(self) -> int:
        """Start all configured MCP servers and discover their tools.

        Returns:
            Number of tools discovered and registered.
        """
        # Start and initialize all servers in parallel
        results = await asyncio.gather(
            *(self._start_one(cfg) for cfg in self._configs),
            return_exceptions=True,
        )

        tool_count = 0
        for i, result in enumerate(results):
            cfg = self._configs[i]
            if isinstance(result, Exception):
                print(f"[MCP] Server '{cfg.name}' failed to start: {result}")
                continue
            if result is None:
                continue

            client = result
            self._servers[cfg.name] = client

            # Discover and register tools
            tools = await client.list_tools()
            for tool_def in tools:
                adapter = MCPToolAdapter(client, tool_def, cfg.name)
                self._tools.append(adapter)
                tool_count += 1

        return tool_count

    async def stop_all(self):
        """Stop all managed MCP server connections."""
        tasks = [client.stop() for client in self._servers.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._servers.clear()
        self._tools.clear()

    async def _start_one(self, cfg: MCPServerConfig) -> Optional[MCPClient]:
        """Start and initialize a single MCP server."""
        client = MCPClient(cfg)
        if not await client.start():
            print(f"[MCP] Server '{cfg.name}' failed to start subprocess")
            return None
        if not await client.initialize():
            print(f"[MCP] Server '{cfg.name}' failed to initialize")
            await client.stop()
            return None
        return client


async def register_mcp_tools_from_config(
    config_path: Optional[str] = None,
) -> Optional[MCPServerManager]:
    """Load MCP server config, connect to servers, and register all tools.

    This is the main entry point for MCP integration. Call it during
    agent initialization to make all configured MCP tools available.

    Args:
        config_path: Path to MCP servers JSON config file.
                     Defaults to ~/.coding-agent/mcp_servers.json

    Returns:
        MCPServerManager if servers were configured and started, None otherwise.
        The caller should keep the manager reference for cleanup (stop_all()).
    """
    config = _load_mcp_config(config_path)
    servers_cfg = config.get("mcpServers", {})

    if not servers_cfg:
        return None

    server_configs = []
    for name, cfg in servers_cfg.items():
        if not isinstance(cfg, dict):
            print(f"[MCP] Invalid config for server '{name}', skipping")
            continue
        env = cfg.get("env", {})
        env = {k: os.path.expandvars(str(v)) for k, v in env.items()} if env else None
        server_configs.append(
            MCPServerConfig(
                name=name,
                command=cfg.get("command", ""),
                args=cfg.get("args", []),
                env=env,
            )
        )

    if not server_configs:
        return None

    manager = MCPServerManager(server_configs)
    tool_count = await manager.start_all()

    # Register all discovered tools in the global registry
    for tool in manager.tools:
        registry.register(tool)

    if tool_count > 0:
        print(f"[MCP] Registered {tool_count} tool(s) from {len(manager._servers)} server(s)")

    return manager
