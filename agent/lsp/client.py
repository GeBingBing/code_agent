"""Lightweight LSP client — JSON-RPC over stdio.

No external dependencies. Communicates with language servers (gopls, pyright,
typescript-language-server, etc.) via the Language Server Protocol.
"""

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Optional


# ── Language server detection ──────────────────────────────────────

# Priority: which executable to try first for each language
LANGUAGE_SERVERS: dict[str, list[str]] = {
    ".py": ["pyright-langserver", "pylsp"],
    ".go": ["gopls"],
    ".ts": ["typescript-language-server", "ts_ls"],
    ".tsx": ["typescript-language-server", "ts_ls"],
    ".js": ["typescript-language-server", "ts_ls"],
    ".jsx": ["typescript-language-server", "ts_ls"],
    ".rs": ["rust-analyzer"],
    ".java": ["jdtls"],
    ".json": ["vscode-json-languageserver"],
}


def detect_server(file_path: str) -> Optional[tuple[str, list[str]]]:
    """Detect which LSP server to use for a file. Returns (command, [args])."""
    ext = Path(file_path).suffix.lower()
    candidates = LANGUAGE_SERVERS.get(ext, [])
    for cmd in candidates:
        if shutil.which(cmd):
            return (cmd, [])
    # Try pyright as universal fallback for Python
    if ext == ".py":
        # Try npx-based pyright
        if shutil.which("npx"):
            return ("npx", ["-y", "pyright", "--stdio"])
    return None


# ── LSP JSON-RPC client ────────────────────────────────────────────

class LSPClient:
    """Async LSP client over stdio.

    Usage:
        client = LSPClient("gopls", [])
        await client.start()
        await client.initialize("/project/root")
        result = await client.request("textDocument/definition", {...})
        await client.shutdown()
    """

    def __init__(self, command: str, args: list[str] = None):
        self.command = command
        self.args = args or []
        self.process: Optional[asyncio.subprocess.Process] = None
        self._buffer = b""
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._reader_task: Optional[asyncio.Task] = None
        self._initialized = False

    async def start(self, root_uri: str = None) -> bool:
        """Start the language server process."""
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._reader_task = asyncio.create_task(self._read_responses())
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    async def initialize(self, root_path: str) -> dict:
        """Send initialize request and didOpen for workspace."""
        root_uri = Path(root_path).resolve().as_uri()
        result = await self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {"linkSupport": False},
                    "references": {},
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                    "callHierarchy": {},
                },
                "workspace": {"symbol": {}},
            },
        }, timeout=30)
        if result:
            self._initialized = True
            # Send initialized notification
            self._send_notification("initialized", {})
        return result or {}

    async def _read_responses(self):
        """Background task: read responses from server stdout."""
        assert self.process and self.process.stdout
        try:
            while True:
                # Read Content-Length header
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    chunk = await self.process.stdout.read(1)
                    if not chunk:
                        return
                    header += chunk

                # Parse Content-Length
                header_str = header.decode("ascii", errors="replace")
                content_length = 0
                for line in header_str.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                        break

                # Read body
                body = b""
                while len(body) < content_length:
                    chunk = await self.process.stdout.read(content_length - len(body))
                    if not chunk:
                        return
                    body += chunk

                # Parse and dispatch
                try:
                    message = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                msg_id = message.get("id")
                if msg_id is not None and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if "error" in message:
                        future.set_exception(
                            LSPError(message["error"].get("message", "LSP error")))
                    else:
                        future.set_result(message.get("result"))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def request(self, method: str, params: dict, timeout: int = 10) -> Optional[dict]:
        """Send a JSON-RPC request and await the response."""
        if not self.process or self.process.returncode is not None:
            return None

        req_id = self._next_id
        self._next_id += 1

        request = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        })

        # Send with Content-Length header (LSP spec)
        body = request.encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(header + body)
        await self.process.stdin.drain()

        # Wait for response
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return None

    def _send_notification(self, method: str, params: dict):
        """Send a JSON-RPC notification (no response expected)."""
        if not self.process or self.process.returncode is not None:
            return
        notification = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })
        body = notification.encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(header + body)

    async def did_open(self, file_path: str):
        """Notify server that a file is open."""
        uri = Path(file_path).resolve().as_uri()
        try:
            content = Path(file_path).read_text("utf-8")
        except Exception:
            return
        self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": _guess_language(file_path),
                "version": 1,
                "text": content,
            },
        })

    async def shutdown(self):
        """Shut down the language server gracefully."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self.process and self.process.returncode is None:
            try:
                await self.request("shutdown", {}, timeout=3)
            except Exception:
                pass
            self._send_notification("exit", {})
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None


class LSPError(Exception):
    """Error from LSP server."""
    pass


def _guess_language(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    return {
        ".py": "python",
        ".go": "go",
        ".ts": "typescript",
        ".tsx": "typescriptreact",
        ".js": "javascript",
        ".jsx": "javascriptreact",
        ".rs": "rust",
        ".java": "java",
        ".json": "json",
    }.get(ext, "plaintext")


# ── Server pool (lazy start, cached) ────────────────────────────────

_server_cache: dict[str, LSPClient] = {}


async def get_lsp_client(file_path: str) -> Optional[LSPClient]:
    """Get or create an LSP client for the given file's language."""
    detected = detect_server(file_path)
    if not detected:
        return None

    cmd, args = detected
    cache_key = cmd

    if cache_key in _server_cache:
        client = _server_cache[cache_key]
        if client.is_running:
            return client

    # Start new server
    client = LSPClient(cmd, args)
    if not await client.start():
        return None

    # Find workspace root (nearest dir with .git or pyproject.toml, etc.)
    workspace_root = _find_workspace_root(file_path)

    await client.initialize(workspace_root)

    _server_cache[cache_key] = client
    return client


def _find_workspace_root(file_path: str) -> str:
    """Find project root by looking for .git, pyproject.toml, go.mod, etc."""
    markers = [".git", "pyproject.toml", "go.mod", "package.json",
               "Cargo.toml", "pom.xml"]
    current = Path(file_path).resolve().parent
    while current != current.parent:
        for marker in markers:
            if (current / marker).exists():
                return str(current)
        current = current.parent
    return str(Path(file_path).resolve().parent)


async def shutdown_all():
    """Shut down all cached LSP servers."""
    for client in _server_cache.values():
        await client.shutdown()
    _server_cache.clear()
