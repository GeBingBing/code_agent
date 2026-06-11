"""MCP Client — JSON-RPC over stdio transport.

Implements the Model Context Protocol for connecting to external tool servers.
Reference: https://modelcontextprotocol.io/
"""

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server connection."""
    name: str
    command: str  # e.g. "npx", "python"
    args: List[str]  # e.g. ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
    env: Optional[Dict[str, str]] = None


class MCPClient:
    """Client for a single MCP server over stdio transport.

    Lifecycle:
        1. start() → spawn subprocess
        2. initialize() → handshake
        3. list_tools() → discover available tools
        4. call_tool(name, args) → invoke a tool
        5. stop() → cleanup
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._req_id = 0
        self._ready = False
        self._lock = asyncio.Lock()

    async def start(self) -> bool:
        """Start the MCP server subprocess."""
        if self._proc is not None:
            return True

        try:
            cmd = [self.config.command] + self.config.args
            env = os.environ.copy()
            if self.config.env:
                env.update(self.config.env)

            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            return True
        except Exception as e:
            print(f"[MCP] Failed to start server '{self.config.name}': {e}")
            return False

    async def initialize(self) -> bool:
        """Perform MCP initialization handshake."""
        if self._proc is None:
            if not await self.start():
                return False

        protocol_version = "2024-11-05"
        result = await self._request(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "coding-agent", "version": "1.0.0"},
            },
        )
        if result is None:
            return False

        # Send initialized notification
        await self._send_notification("notifications/initialized")
        self._ready = True
        return True

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools from the MCP server."""
        if not self._ready:
            if not await self.initialize():
                return []

        result = await self._request("tools/list", {})
        if result and isinstance(result, dict):
            return result.get("tools", [])
        return []

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call an MCP tool by name with arguments."""
        if not self._ready:
            if not await self.initialize():
                return {"error": "MCP client not initialized"}

        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        return result or {"error": "No response from MCP server"}

    async def stop(self):
        """Stop the MCP server subprocess."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        except Exception:
            pass
        finally:
            self._proc = None
            self._ready = False

    # ── Internal JSON-RPC helpers ───────────────────────────────────────

    async def _request(self, method: str, params: Dict[str, Any]) -> Any:
        async with self._lock:
            self._req_id += 1
            req_id = self._req_id
            msg = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }

            payload = json.dumps(msg) + "\n"
            try:
                self._proc.stdin.write(payload.encode("utf-8"))
                await self._proc.stdin.drain()
            except Exception as e:
                print(f"[MCP] Write error: {e}")
                return None

            # Read response lines until we find matching id
            while True:
                try:
                    line = await asyncio.wait_for(
                        self._proc.stdout.readline(), timeout=30
                    )
                except asyncio.TimeoutError:
                    print(f"[MCP] Read timeout for request {req_id}")
                    return None

                if not line:
                    return None

                try:
                    resp = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                # Skip notifications (no id)
                if "id" not in resp:
                    continue

                if resp.get("id") == req_id:
                    if "error" in resp:
                        print(f"[MCP] Error: {resp['error']}")
                        return None
                    return resp.get("result")

    async def _send_notification(self, method: str, params: Optional[Dict[str, Any]] = None):
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        payload = json.dumps(msg) + "\n"
        try:
            self._proc.stdin.write(payload.encode("utf-8"))
            await self._proc.stdin.drain()
        except Exception:
            pass


async def _test_client():
    """Quick sanity check — requires an MCP server to be installed."""
    config = MCPServerConfig(
        name="test",
        command="echo",
        args=["{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"tools\":[]}}"],
    )
    client = MCPClient(config)
    ok = await client.start()
    print("start:", ok)
    await client.stop()


if __name__ == "__main__":
    asyncio.run(_test_client())
