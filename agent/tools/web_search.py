"""Web search tool — Bing primary, DDG fallback."""

import re
from typing import Optional

import httpx

from .base import BaseTool, ToolResult, registry


def _clean_html(text: str) -> str:
    """Decode HTML entities and clean whitespace."""
    import html as _html
    text = _html.unescape(text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


class WebSearchTool(BaseTool):
    user_facing_name = "Search"

    is_concurrency_safe = True
    is_read_only = True
    name = "web_search"
    description = "Search the web for information (news, docs, code examples, etc.)"

    async def execute(
        self,
        query: str,
        max_results: int = 10,
        max_length: int = 4000,
        **kwargs,
    ) -> ToolResult:
        """Search the web and return results."""
        from urllib.parse import quote

        # Try Bing first (works in China), fall back to DuckDuckGo
        results = None
        error = None

        for backend in ("bing", "ddg"):
            try:
                if backend == "bing":
                    results = await self._search_bing(query, max_results)
                else:
                    results = await self._search_ddg(query, max_results)
                if results:
                    break
            except Exception as e:
                error = str(e)[:200]
                continue

        if not results:
            return ToolResult(
                success=False, content="",
                error=f"Search failed: {error or 'no results from any backend'}"
            )

        lines = [f"Search results for: {query}", ""]
        for i, (title, url, snippet) in enumerate(results, 1):
            lines.append(f"{i}. {title}")
            lines.append(f"   URL: {url}")
            lines.append(f"   {snippet}")
            lines.append("")

        content = "\n".join(lines)
        if len(content) > max_length:
            content = content[:max_length] + f"\n\n... [truncated]"

        return ToolResult(success=True, content=content)

    # ── Bing ─────────────────────────────────────────────────────

    async def _search_bing(self, query: str, max_results: int) -> list:
        """Search Bing and return (title, url, snippet) tuples."""
        from urllib.parse import quote
        url = f"https://www.bing.com/search?q={quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text

        results = []
        skip_domains = ("bing.com", "microsoft.com/bing", "go.microsoft.com", "r.bing.com")

        blocks = re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.DOTALL)
        for block in blocks:
            if len(results) >= max_results:
                break

            # Title: from <h2> (strip all HTML tags inside)
            title_m = re.search(r'<h2[^>]*>(.*?)</h2>', block, re.DOTALL)
            if not title_m:
                continue
            title = _clean_html(re.sub(r'<[^>]+>', '', title_m.group(1)))

            # URL: first external http(s) link in the block
            href = ""
            for m in re.finditer(r'href="(https?://[^"]+)"', block):
                url_candidate = m.group(1)
                if any(skip in url_candidate for skip in skip_domains):
                    continue
                href = url_candidate
                break

            if not href or not title:
                continue

            # Snippet: from <p> in the block, or b_caption div
            snippet = ""
            snippet_m = re.search(r'<p[^>]*>(.{20,400}?)</p>', block, re.DOTALL)
            if snippet_m:
                snippet = _clean_html(re.sub(r'<[^>]+>', '', snippet_m.group(1)))

            results.append((title, href, snippet))

        return results

    # ── DuckDuckGo (fallback) ────────────────────────────────────

    async def _search_ddg(self, query: str, max_results: int) -> list:
        """Search DuckDuckGo HTML and return (title, url, snippet) tuples."""
        from urllib.parse import quote
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Coding-Agent; compatible)"}

        async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text

        results = []
        result_links = re.findall(
            r'<a class="result__a" href="([^"]+)"[^>]*>([^<]*)</a>', html, re.IGNORECASE
        )
        result_snippets = re.findall(
            r'<a class="result__snippet"[^>]*>([^<]*(?:<[^>]+>[^<]*)*)</a>', html, re.IGNORECASE | re.DOTALL
        )

        clean_snippets = []
        for s in result_snippets[:max_results]:
            clean_snippets.append(_clean_html(re.sub(r'<[^>]+>', '', s)))

        for i, (url, title) in enumerate(result_links[:max_results]):
            title = re.sub(r'<[^>]+>', '', title).strip()
            snippet = clean_snippets[i] if i < len(clean_snippets) else ""
            if url.startswith("http"):
                results.append((title, url, snippet))

        return results

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
                        "query": {"type": "string", "description": "Search query"},
                        "max_results": {
                            "type": "integer", "default": 10,
                            "description": "Max number of results",
                        },
                        "max_length": {
                            "type": "integer", "default": 4000,
                            "description": "Max total characters to return",
                        },
                    },
                    "required": ["query"],
                },
            },
        }


registry.register(WebSearchTool())
