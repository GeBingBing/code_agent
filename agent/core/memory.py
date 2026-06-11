"""Memory system - L1 working memory, L2 summaries, L3 long-term memory.

Integrates vector memory for semantic search of long-term memories.
Context compression uses token-budget-based, semantic-aware strategy.
"""

import json
import os
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from dataclasses import dataclass, field

import numpy as np

# Try tiktoken for accurate token counting
try:
    import tiktoken
    _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
except (ImportError, Exception):
    _TIKTOKEN_ENC = None


@dataclass
class MemoryMessage:
    role: str
    content: str
    tool_call_id: Optional[str] = None
    tool_calls: Optional[str] = None  # JSON string of tool_calls for assistant messages
    relevance_score: float = 0.0  # Semantic relevance score (0-1) used for priority sorting
    metadata: Dict[str, str] = field(default_factory=dict)  # Additional metadata (e.g., file_path, task_id)


class MemoryManager:
    """Three-layer memory system.

    L1: Working memory - conversation history, auto-trimmed.
    L2: Session summaries - compressed when exceeding token threshold.
    L3: Long-term memory - persisted to ~/.coding-agent/memory.md + VectorMemory for semantic search
    """

    def __init__(
        self,
        max_tokens: int = 15000,
        memory_dir: Optional[str] = None,
    ):
        self.max_tokens = max_tokens
        self.memory_dir = Path(memory_dir or os.path.expanduser("~/.coding-agent"))
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.memory_dir / "memory.md"

        # L1: Working memory
        self.working_memory: List[MemoryMessage] = []

        # L2: Session summaries
        self.summaries: List[str] = []

        # L3: Long-term memory (text-based)
        self.long_term = self._load_long_term()

        # L3+: Vector memory for semantic search
        self._vector_memory = None  # Lazy initialization

    @property
    def vector_memory(self):
        """Lazy-load vector memory."""
        if self._vector_memory is None:
            from .vector_memory import get_vector_memory
            self._vector_memory = get_vector_memory()
        return self._vector_memory

    def _load_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def _save_long_term(self):
        self.memory_file.write_text(self.long_term, encoding="utf-8")

    def add(self, role: str, content: str, tool_call_id: Optional[str] = None, tool_calls: Optional[str] = None):
        """Add a message to working memory and compress if needed."""
        self.working_memory.append(MemoryMessage(role, content, tool_call_id, tool_calls))
        if self._estimate_tokens() > self.max_tokens:
            self._compress()

    @staticmethod
    def _count_tokens(text: str) -> int:
        """Count tokens in text. Uses tiktoken if available, otherwise CJK-aware heuristic."""
        if _TIKTOKEN_ENC:
            return len(_TIKTOKEN_ENC.encode(text))
        # Fallback: CJK chars ≈ 1 token, non-CJK ≈ 1/3 token each
        cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
        return cjk + max(1, (len(text) - cjk) // 3)

    def _estimate_tokens(self) -> int:
        """Token estimation for working memory (content + overhead)."""
        total = 0
        for m in self.working_memory:
            total += self._count_tokens(m.content) + 4  # role + metadata overhead
            if m.tool_calls:
                total += self._count_tokens(m.tool_calls)
            if m.tool_call_id:
                total += len(m.tool_call_id) // 3
        return total

    def compact(self, summary: str):
        """Compact working memory: replace old messages with a summary.

        Called by engine when approaching context window limit.
        """
        # Keep system message + last 6 messages + new summary
        system_msgs = [m for m in self.working_memory if m.role == "system"]
        recent = self.working_memory[-6:]
        # Build new working memory
        self.working_memory = system_msgs + [
            MemoryMessage("user", f"[Context summary]\n{summary}", None, None)
        ] + recent

    def _compress(self):
        """Semantic-aware context compression.

        Strategy (in priority order):
        1. System messages are NEVER compressed — always keep
        2. Walk backward from end, keeping messages until token budget reached
        3. Ensure tool_call/tool_result pairs stay together
        4. Large tool outputs (read_file > 200 lines, execute_command > 500 chars)
           are truncated to a preview instead of being fully retained
        5. Compressed messages go into L2 summaries
        """
        if not self.working_memory:
            return

        # Target: keep ~70% of max_tokens for working memory
        target_tokens = int(self.max_tokens * 0.7)
        if self._estimate_tokens() <= target_tokens:
            return

        # Step 1: Find system messages — they stay
        n = len(self.working_memory)
        system_count = 0
        for msg in self.working_memory:
            if msg.role == "system":
                system_count += 1
            else:
                break

        # Step 2: Walk backward from end, accumulate until budget
        keep_from = n  # Messages from this index onward are kept
        token_count = 0
        kept_tc_ids = set()  # tool_call IDs in the kept region
        kept_tr_ids = set()  # tool_call_ids from kept tool results

        for i in range(n - 1, system_count - 1, -1):
            msg = self.working_memory[i]
            msg_tokens = self._count_tokens(msg.content) + 4

            if msg.tool_calls:
                msg_tokens += self._count_tokens(msg.tool_calls or "")
                try:
                    for tc in json.loads(msg.tool_calls or "[]"):
                        if tc.get("id"):
                            kept_tc_ids.add(tc["id"])
                except (json.JSONDecodeError, KeyError):
                    pass
            if msg.tool_call_id:
                kept_tr_ids.add(msg.tool_call_id)

            if token_count + msg_tokens > target_tokens:
                # Budget exceeded — stop here (but ensure pair integrity below)
                keep_from = i + 1
                break

            token_count += msg_tokens
            keep_from = i
        else:
            keep_from = system_count  # All non-system messages fit

        # Step 3: Walk backward from keep_from to capture paired tool_call/tool_result
        for i in range(keep_from - 1, system_count - 1, -1):
            msg = self.working_memory[i]
            if msg.tool_call_id and msg.tool_call_id in kept_tc_ids:
                keep_from = i
            if msg.tool_calls:
                try:
                    for tc in json.loads(msg.tool_calls or "[]"):
                        if tc.get("id") in kept_tr_ids:
                            keep_from = i
                            kept_tc_ids.add(tc.get("id", ""))
                except (json.JSONDecodeError, KeyError):
                    pass

        # Step 4: Compress messages before keep_from
        if keep_from <= system_count:
            return

        older = self.working_memory[system_count:keep_from]

        # Build L2 summary with smart truncation of large tool outputs
        lines = []
        for msg in older:
            content = msg.content
            # Truncate large read_file outputs
            if msg.role == "tool" and msg.content.count('\n') > 200:
                head = "\n".join(msg.content.split('\n')[:20])
                content = f"{head}\n... ({msg.content.count(chr(10)) - 20} more lines)"
            # Truncate large command outputs
            elif msg.role == "tool" and len(msg.content) > 500:
                content = msg.content[:500] + f"... ({len(msg.content) - 500} more chars)"
            # General message preview
            elif len(msg.content) > 300:
                content = msg.content[:300].replace("\n", " ")
            else:
                content = msg.content[:300].replace("\n", " ")

            lines.append(f"{msg.role}: {content}")

        summary = "\n".join(lines)
        self.summaries.append(summary)

        # Keep system messages + compressed region onward
        self.working_memory = self.working_memory[:system_count] + self.working_memory[keep_from:]

    def get_messages(self) -> List[MemoryMessage]:
        """Return full context: summaries + working memory."""
        messages = []
        for summary in self.summaries:
            messages.append(MemoryMessage(role="user", content=f"[Earlier summary] {summary}"))
        messages.extend(self.working_memory)
        return messages

    def get_long_term_context(self) -> str:
        """Return long-term memory for injecting into system prompt."""
        if not self.long_term:
            return ""
        return f"\n\n[Long-term memory]\n{self.long_term}"

    # PR-14: pinned entries are exempt from the 50-entry LRU cap.
    # Used for identity facts (user.name etc.) so they survive flooding
    # from auto-recorded tool calls (last_written_file, etc.).
    # Configurable upper bound via env CODING_AGENT_MEMORY_PINNED_MAX.
    _PINNED_MAX = 200

    def remember(self, key: str, value: str, pinned: bool = False):
        """Store a fact into long-term memory with deduplication by key.

        Args:
            key: Unique identifier for the fact (e.g. "user_name")
            value: The fact value (any string)
            pinned: If True, this entry is exempt from the 50-entry LRU
                cap. Use for identity facts that must never be evicted.

        Also stores in vector memory for semantic search.

        Multi-line values are sanitized (newlines → ↵) to preserve the
        one-line-per-entry format. This prevents a multi-line value from
        corrupting the entire memory.md file.
        """
        # Sanitize multi-line values: replace \n with the visual marker ↵
        # to keep memory.md as a clean line-based format.
        safe_value = str(value).replace("\r\n", "\n").replace("\n", "\u21B5")
        # Strip control characters that could break the format
        safe_value = "".join(c for c in safe_value if c == "\u21B5" or (c >= " " and c != "\x7f"))
        if not safe_value:
            return

        # Pinned entries get a marker so we can identify them on read
        marker = "📌 " if pinned else ""
        entry = f"- {marker}{key}: {safe_value}"

        # If key already exists, replace its value
        lines = [l for l in self.long_term.strip().split('\n') if l] if self.long_term else []
        new_lines = []
        # Match both pinned and unpinned variants of the same key
        key_prefix_pinned = f"- 📌 {key}:"
        key_prefix_unpinned = f"- {key}:"
        replaced = False
        for line in lines:
            if line.startswith(key_prefix_pinned) or line.startswith(key_prefix_unpinned):
                if not replaced:
                    new_lines.append(entry)
                    replaced = True
                # Skip old entries for the same key
            else:
                new_lines.append(line)

        if not replaced:
            new_lines.append(entry)

        # Cap behavior: pinned and unpinned have INDEPENDENT caps.
        # This is the critical fix: when adding an unpinned entry that
        # overflows the 50-cap, we trim OTHER unpinned entries, never
        # pinned ones. Pinned entries are tracked and capped separately
        # at _PINNED_MAX (default 200).
        pinned_lines = [l for l in new_lines if l.startswith("- 📌 ")]
        unpinned_lines = [l for l in new_lines if not l.startswith("- 📌 ")]

        # Apply caps to each category independently
        if len(unpinned_lines) > 50:
            unpinned_lines = unpinned_lines[-50:]
        if len(pinned_lines) > self._PINNED_MAX:
            pinned_lines = pinned_lines[-self._PINNED_MAX:]

        # Combine: pinned first, then unpinned
        new_lines = pinned_lines + unpinned_lines

        self.long_term = '\n'.join(new_lines) + ('\n' if new_lines else '')
        self._save_long_term()

        # Also store in vector memory for semantic search
        try:
            self.vector_memory.add(key, value)
        except Exception:
            pass  # Don't fail if vector memory fails

    def is_pinned(self, entry: str) -> bool:
        """Check if a memory.md line is pinned (📌 marker)."""
        return entry.strip().startswith("- 📌 ")

    def search_long_term(self, query: str, top_k: int = 3) -> List[Tuple[str, str, float]]:
        """Semantic search over long-term memory.

        Args:
            query: Search query
            top_k: Number of results to return

        Returns:
            List[(key, value, similarity)]
        """
        try:
            return self.vector_memory.search(query, top_k)
        except Exception:
            return []

    def compute_relevance_scores(self, context_keywords: List[str]) -> None:
        """Compute semantic relevance scores for all working memory messages.

        Scores are based on keyword overlap with the current context.
        Higher scores indicate more relevant messages for retention.

        Args:
            context_keywords: Keywords extracted from recent conversation context
        """
        if not context_keywords:
            return

        for msg in self.working_memory:
            if msg.role == "system":
                # System messages always have highest priority
                msg.relevance_score = 1.0
                continue

            # Compute keyword overlap score
            content_lower = msg.content.lower()
            matches = sum(1 for kw in context_keywords if kw.lower() in content_lower)
            score = matches / len(context_keywords) if context_keywords else 0.0

            # Boost score for recent messages
            msg_idx = self.working_memory.index(msg)
            recency_boost = 0.1 * (msg_idx / max(1, len(self.working_memory)))
            msg.relevance_score = min(1.0, score + recency_boost)

    def get_prioritized_messages(self, max_tokens: int, context_keywords: Optional[List[str]] = None) -> List[MemoryMessage]:
        """Get prioritized messages within token budget.

        Combines semantic relevance scoring with token budget to select
        the most valuable messages for the current context.

        Args:
            max_tokens: Maximum tokens to spend
            context_keywords: Optional keywords for relevance scoring

        Returns:
            List of prioritized MemoryMessages within token budget
        """
        if context_keywords:
            self.compute_relevance_scores(context_keywords)

        # Sort by relevance (descending), then by recency
        sorted_msgs = sorted(
            self.working_memory,
            key=lambda m: (m.relevance_score, self.working_memory.index(m)),
            reverse=True
        )

        # Select messages within token budget, preferring high-relevance ones
        result = []
        total_tokens = 0

        for msg in sorted_msgs:
            msg_tokens = self._count_tokens(msg.content) + 4
            if msg.tool_calls:
                msg_tokens += self._count_tokens(msg.tool_calls)
            if msg.tool_call_id:
                msg_tokens += len(msg.tool_call_id) // 3

            # Reserve budget for system messages
            if msg.role == "system":
                if total_tokens + msg_tokens <= max_tokens:
                    result.append(msg)
                    total_tokens += msg_tokens
                continue

            if total_tokens + msg_tokens <= max_tokens:
                result.append(msg)
                total_tokens += msg_tokens

        # Sort back to original order for coherent conversation flow
        result.sort(key=lambda m: self.working_memory.index(m))
        return result

    def semantic_compress(self, query: str, target_tokens: int) -> None:
        """Semantic-aware compression using vector similarity.

        Instead of simple truncation, this method:
        1. Embeds the query context
        2. Computes similarity with each message
        3. Retains high-similarity messages fully
        4. Truncates low-similarity messages to previews

        Args:
            query: Current task/query context
            target_tokens: Target token budget for compressed output
        """
        if not self.working_memory:
            return

        # Get current token count
        current_tokens = self._estimate_tokens()
        if current_tokens <= target_tokens:
            return

        # Find system messages (always keep)
        system_count = 0
        for msg in self.working_memory:
            if msg.role == "system":
                system_count += 1
            else:
                break

        # Compute similarity scores for each message
        try:
            from .vector_memory import simple_text_hash
            query_emb = simple_text_hash(query, dim=128)
        except Exception:
            # Fallback if vector hashing fails
            return self._compress()

        scored_messages = []
        for i, msg in enumerate(self.working_memory[system_count:], start=system_count):
            try:
                msg_emb = simple_text_hash(msg.content, dim=128)
                similarity = float(np.dot(query_emb, msg_emb))
            except Exception:
                similarity = 0.0

            msg_tokens = self._count_tokens(msg.content) + 4
            scored_messages.append((i, msg, similarity, msg_tokens))

        # Sort by similarity (descending) - high similarity = keep
        scored_messages.sort(key=lambda x: x[2], reverse=True)

        # Select messages within budget, prioritizing high-similarity ones
        kept_indices = set(range(system_count))  # Always keep system messages
        token_budget = target_tokens

        # First pass: add system messages
        for i in range(system_count):
            token_budget -= scored_messages[i][3]

        # Second pass: greedily add high-similarity messages until budget exhausted
        for _, msg, similarity, msg_tokens in scored_messages[system_count:]:
            if token_budget >= msg_tokens:
                kept_indices.add(self.working_memory.index(msg))
                token_budget -= msg_tokens
            else:
                # Truncate this message
                break

        # Build summary for dropped messages
        dropped = []
        for i, msg, _, _ in scored_messages:
            if i not in kept_indices:
                dropped.append(msg)

        if dropped:
            summary_parts = []
            for msg in dropped:
                content = msg.content
                if len(content) > 200:
                    content = content[:200] + "..."
                summary_parts.append(f"[{msg.role}] {content}")

            self.summaries.append(f"[Semantic compression] {len(dropped)} messages summarized:\n" + "\n".join(summary_parts))

        # Rebuild working memory with kept messages
        kept_messages = [msg for i, msg in enumerate(self.working_memory) if i in kept_indices]
        self.working_memory = kept_messages

    def clear_working_memory(self):
        """Clear working memory (e.g. between sessions)."""
        self.working_memory = []
        self.summaries = []
