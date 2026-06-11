"""Code retrieval - hybrid search over indexed codebase."""

import time
from typing import List, Optional
from dataclasses import dataclass

from .code_indexer import CodeIndexer


@dataclass
class SearchResult:
    path: str
    kind: str
    name: str
    line: int
    snippet: str
    score: float


class CodeRetriever:
    """Retrieve relevant code using hybrid scoring (filename + symbols + content)."""

    def __init__(self, indexer: CodeIndexer):
        self.indexer = indexer

    def search(self, query: str, top_k: int = 5, language: Optional[str] = None) -> List[SearchResult]:
        """Search the indexed codebase."""
        start = time.time()
        query_lower = query.lower()
        query_parts = query_lower.split()
        results: List[SearchResult] = []

        for path, file_idx in self.indexer.files.items():
            if language and file_idx.language != language:
                continue

            # 1. Filename match (high weight)
            filename_score = 0
            if query_lower in path.lower():
                filename_score = 10
            for part in query_parts:
                if part in path.lower():
                    filename_score += 3

            if filename_score > 0:
                results.append(SearchResult(
                    path=path,
                    kind="file",
                    name=path,
                    line=1,
                    snippet=f"File: {path}",
                    score=filename_score,
                ))

            # 2. Symbol match (medium-high weight)
            for sym in file_idx.symbols:
                sym_score = 0
                sym_name_lower = sym.name.lower()
                if query_lower in sym_name_lower:
                    sym_score = 8
                else:
                    for part in query_parts:
                        if part in sym_name_lower:
                            sym_score += 2

                if sym_score > 0:
                    snippet = self._get_snippet(file_idx.lines, sym.line)
                    results.append(SearchResult(
                        path=path,
                        kind=sym.kind,
                        name=sym.name,
                        line=sym.line,
                        snippet=snippet,
                        score=sym_score,
                    ))

            # 3. Content line match (low weight)
            for i, line in enumerate(file_idx.lines):
                line_lower = line.lower()
                content_score = 0
                if query_lower in line_lower:
                    content_score = 2
                else:
                    match_count = sum(1 for p in query_parts if p in line_lower)
                    if match_count >= len(query_parts) // 2 + 1:
                        content_score = 1

                if content_score > 0:
                    results.append(SearchResult(
                        path=path,
                        kind="line",
                        name=line.strip()[:80],
                        line=i + 1,
                        snippet=line.strip()[:200],
                        score=content_score,
                    ))

        # Deduplicate by (path, line)
        seen = set()
        deduped = []
        for r in results:
            key = (r.path, r.line, r.name)
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        # Sort by score and return top_k
        deduped.sort(key=lambda x: x.score, reverse=True)
        elapsed = (time.time() - start) * 1000

        # Append timing info to the last result's snippet for debug
        if deduped:
            deduped[0].snippet += f"\n  (search took {elapsed:.1f}ms)"

        return deduped[:top_k]

    def semantic_search(self, query: str, top_k: int = 5, include_related: bool = True) -> List[SearchResult]:
        """Semantic-aware search that includes related symbols.

        Performs regular search first, then augments results with:
        - References to matched symbols
        - Related symbols (same class, callers, callees)
        """
        base_results = self.search(query, top_k=top_k * 2)
        if not base_results or not include_related:
            return base_results[:top_k]

        # Collect matched symbol names
        matched_names = set()
        for r in base_results:
            if r.kind in ("function", "method", "class"):
                matched_names.add(r.name)

        extra_results: List[SearchResult] = []
        seen_keys = {(r.path, r.line, r.name) for r in base_results}

        for name in matched_names:
            # Add references
            refs = self.indexer.find_references(name)
            for ref in refs:
                key = (ref["path"], ref["line"], ref["context"])
                if key not in seen_keys:
                    seen_keys.add(key)
                    extra_results.append(SearchResult(
                        path=ref["path"],
                        kind="reference",
                        name=f"ref: {name}",
                        line=ref["line"],
                        snippet=f"  {ref['line']:4d} | {ref['context']}",
                        score=3.0,
                    ))

            # Add related symbols
            related = self.indexer.get_related_symbols(name)
            for rel in related:
                key = (rel["path"], rel["line"], rel["name"])
                if key not in seen_keys:
                    seen_keys.add(key)
                    snippet = self._get_snippet(
                        self.indexer.files[rel["path"]].lines, rel["line"]
                    ) if rel["path"] in self.indexer.files else ""
                    extra_results.append(SearchResult(
                        path=rel["path"],
                        kind=rel.get("kind", "related"),
                        name=f"{rel['relation']}: {rel['name']}",
                        line=rel["line"],
                        snippet=snippet,
                        score=4.0 if rel["relation"] == "same_class" else 3.5,
                    ))

        all_results = base_results + extra_results
        all_results.sort(key=lambda x: x.score, reverse=True)
        return all_results[:top_k]

    def _get_snippet(self, lines: List[str], line_no: int, context: int = 2) -> str:
        """Extract surrounding lines for context."""
        start = max(0, line_no - context - 1)
        end = min(len(lines), line_no + context)
        snippet_lines = []
        for i in range(start, end):
            marker = ">>> " if i == line_no - 1 else "    "
            snippet_lines.append(f"{marker}{i + 1:4d} | {lines[i]}")
        return "\n".join(snippet_lines)
