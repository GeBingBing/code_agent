"""Web fetch tool - retrieve content from URLs for documentation reference."""

import ipaddress
import re
import socket
from typing import Optional
from urllib.parse import urlparse

import httpx

from .base import BaseTool, ToolResult, registry

# Blocked hosts for SSRF prevention
_BLOCKED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "169.254.169.254",  # AWS metadata
    "metadata.google.internal",  # GCP metadata
}

# Blocked CIDR ranges (private networks + link-local)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _validate_url(url: str) -> Optional[str]:
    """Validate URL for SSRF prevention. Returns error message or None if safe."""
    try:
        parsed = urlparse(url)
    except Exception:
        return f"Invalid URL: {url}"

    if parsed.scheme not in ("http", "https"):
        return f"Only http/https URLs are allowed, got: {parsed.scheme}"

    hostname = parsed.hostname
    if not hostname:
        return "URL has no valid hostname"

    hostname_lower = hostname.lower()

    # Block known metadata/internal hosts
    if hostname_lower in _BLOCKED_HOSTS:
        return f"Access to '{hostname}' is blocked"

    # Resolve hostname to IP
    try:
        addr = ipaddress.ip_address(hostname_lower)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(hostname_lower, None)
            ips = {r[4][0] for r in resolved}
        except (socket.gaierror, OSError):
            return f"Cannot resolve hostname: {hostname}"
    else:
        ips = {str(addr)}

    # Check all resolved IPs against blocked networks
    for ip_str in ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        for net in _BLOCKED_NETWORKS:
            if ip in net:
                return f"Access to private/internal IP '{ip_str}' is blocked"
        if ip_str in _BLOCKED_HOSTS:
            return f"Access to '{ip_str}' is blocked"

    return None


class WebFetchTool(BaseTool):
    user_facing_name = "Fetch"

    is_concurrency_safe = True
    is_read_only = True
    name = "web_fetch"
    description = "Fetch content from a URL (documentation, API reference, etc.)"

    async def execute(self, url: str, max_length: int = 8000, **kwargs) -> ToolResult:
        """Fetch and return text content from a URL.

        Args:
            url: URL to fetch
            max_length: Max characters to return (truncates if longer)
        """
        # SSRF validation
        if error := _validate_url(url):
            return ToolResult(success=False, content="", error=error)

        headers = {"User-Agent": "Mozilla/5.0 (Coding-Agent)"}

        try:
            async with httpx.AsyncClient(
                timeout=15.0, headers=headers, follow_redirects=True
            ) as client:
                response = await client.get(url)
                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "")
                raw = response.content

                # Handle binary content
                if "image" in content_type or "pdf" in content_type:
                    return ToolResult(
                        success=True, content=f"Binary content ({content_type}). URL: {url}"
                    )

                # Try UTF-8 first, fallback to latin-1
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1", errors="replace")

                # Simple HTML stripping if it looks like HTML
                if "<html" in text.lower() or "<!doctype" in text.lower():
                    text = self._strip_html(text)

                if len(text) > max_length:
                    text = text[:max_length] + f"\n\n... [truncated, {len(text)} chars total]"

                return ToolResult(success=True, content=text)

        except httpx.TimeoutException:
            return ToolResult(success=False, content="", error="Request timed out after 15s")
        except httpx.HTTPStatusError as e:
            return ToolResult(
                success=False, content="", error=f"HTTP error: {e.response.status_code}"
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

    def _strip_html(self, html: str) -> str:
        """Minimal HTML to text conversion."""
        # Remove script and style blocks
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

        # Replace common block tags with newlines
        html = re.sub(r"</(p|div|h[1-6]|li|tr|pre|blockquote)>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<(br|hr)\s*/?>", "\n", html, flags=re.IGNORECASE)

        # Remove remaining tags
        html = re.sub(r"<[^>]+>", "", html)

        # Decode entities
        import html as html_module

        html = html_module.unescape(html)

        # Collapse whitespace
        lines = [line.strip() for line in html.splitlines()]
        lines = [line for line in lines if line]

        return "\n".join(lines)

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch"},
                        "max_length": {
                            "type": "integer",
                            "default": 8000,
                            "description": "Max characters to return",
                        },
                    },
                    "required": ["url"],
                },
            },
        }


# Register tool
registry.register(WebFetchTool())
